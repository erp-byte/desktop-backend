"""One mail identity for every ERP notification.

WHY
    Every sender set ``From`` to the bare address, so Gmail printed "erp" for a
    customer return, a job-work challan and an inward deletion alike. The sender
    column is fixed-width, always visible and first in the reading path, so it is
    the cheapest place to say which module is talking — and it was empty.

WHAT IT SETS
    From         "Candor · Job Work" <erp@candorfoods.in>   (same mailbox, just labelled)
    Subject      per policy, see below
    List-Id      jobwork.candorfoods.in — Gmail CAN filter this, via `list:`
    X-Candor-*   structured fields for Outlook rules / Gmail API / later tooling
    Auto-Submitted, X-Entity-Ref-ID — see "quiet wins" below

THE SUBJECT POLICY IS THE IMPORTANT PART
    Gmail groups a conversation by normalised subject *plus* References. Change the
    subject between two mails and the thread splits, even when the References chain
    is perfect — ERP's own sample_mail_service documents this the hard way.

    Customer returns and NPD sample requests deliberately thread today: one
    conversation per entity, with the action shown in the body banner. Putting a
    changing event in their subject would shatter those threads.

    So there are two policies:

      ANCHOR  threaded modules. The subject is left EXACTLY as it was, with at most
              a module glyph prefixed — and that glyph is constant per module, never
              per event, so the normalised subject never changes. Threading survives
              untouched, and the mail still carries a visual tag.

      EVENT   standalone mails (job work, inward deletion, daily report, security).
              Full grammar: "<glyph> <ID> — <what changed>". Each mail is its own
              inbox row, which is what these are.

    Nothing here touches Message-ID, In-Reply-To or References, and nothing touches
    mail bodies — the per-event banners and colours already in place keep working.

QUIET WINS
    Auto-Submitted: auto-generated  (RFC 3834) stops out-of-office auto-replies
    bouncing back into the ERP mailbox — a real source of noise on bulk sends.

    X-Entity-Ref-ID is a de-facto Gmail hint that keeps unrelated mails with similar
    subjects OUT of one another's threads. Applied to EVENT mails only, where each
    message is meant to stand alone (two job-work challans should never merge).
    Best-effort: Gmail does not document it, and it is inert everywhere else.
"""
from __future__ import annotations

import os
import re
from email.headerregistry import Address
from email.message import EmailMessage
from enum import Enum

MAIL_DOMAIN = "candorfoods.in"
BRAND = "Candor"

# Test/staging sends get a visible marker so nobody acts on a rehearsal.
MAIL_ENV = (os.getenv("MAIL_ENV") or "PROD").strip().upper()


class Module(str, Enum):
    """Each value is the List-Id label; see MODULES for the rest."""
    RETURNS = "returns"
    NPD = "npd"
    JOB_WORK = "jobwork"
    PRODUCTION = "production"
    INWARD = "inward"
    TRANSFERS = "transfers"
    REPORTS = "reports"
    SECURITY = "security"


# display name shown in the sender column, and the module's constant glyph
MODULES: dict[Module, tuple[str, str]] = {
    Module.RETURNS:    ("Returns",      "↩️"),   # ↩️
    Module.NPD:        ("NPD",          "\U0001F9EA"),     # 🧪
    Module.JOB_WORK:   ("Job Work",     "\U0001F527"),     # 🔧
    Module.PRODUCTION: ("Production",   "\U0001F3ED"),     # 🏭
    Module.INWARD:     ("Inward",       "\U0001F4E5"),     # 📥
    Module.TRANSFERS:  ("Transfers",    "\U0001F504"),     # 🔄
    Module.REPORTS:    ("Daily Report", "\U0001F4CA"),     # 📊
    Module.SECURITY:   ("Security",     "\U0001F510"),     # 🔐
}

# Event glyphs — EVENT-policy subjects only. Never applied to a threaded subject,
# because a glyph that changes per event would change the subject and split the thread.
EVENT_GLYPHS: dict[str, str] = {
    "raised": "\U0001F195",       # 🆕
    "created": "\U0001F195",
    "approved": "✅",         # ✅
    "completed": "✅",
    "issued": "✅",
    "rejected": "❌",         # ❌
    "cancelled": "\U0001F6AB",    # 🚫
    "deleted": "\U0001F5D1️",  # 🗑️
    "dispatched": "\U0001F69A",   # 🚚
    "pending": "⏳",          # ⏳
    "awaiting": "⏳",
    "hold": "⏸️",       # ⏸️
    "action": "⚠️",     # ⚠️
    "escalation": "⚠️",
    "updated": "\U0001F504",      # 🔄
    "ready": "\U0001F4E6",        # 📦
    "report": "\U0001F4CA",       # 📊
}


class SubjectPolicy(str, Enum):
    ANCHOR = "anchor"       # threaded — subject must stay byte-stable
    EVENT = "event"         # standalone — subject describes what changed
    VERBATIM = "verbatim"   # do not touch the subject at all


# VERBATIM exists for replying into a conversation that started before this module
# owned the subject line. Those mails carry no module glyph, so adding one here
# would change the normalised subject and open a second thread instead of
# continuing the first. Identity still applies to From, List-Id and the X-headers.


def _display_name(module: Module) -> str:
    label = MODULES[module][0]
    name = f"{BRAND} · {label}"          # "Candor · Job Work"
    return f"[{MAIL_ENV}] {name}" if MAIL_ENV != "PROD" else name


def _list_id(module: Module) -> str:
    label = MODULES[module][0]
    return f"{BRAND} {label} <{module.value}.{MAIL_DOMAIN}>"


def _glyph_for(status: str | None, module: Module) -> str:
    key = (status or "").strip().lower().replace(" ", "_")
    for token, glyph in EVENT_GLYPHS.items():
        if token in key:
            return glyph
    return MODULES[module][1]


_LEADING_GLYPH = re.compile(
    r"^\s*(?:\[[A-Z]+\]\s*)?[\U0001F300-\U0001FAFF←-⯿️]+\s*"
)


def _already_stamped(subject: str) -> bool:
    return bool(_LEADING_GLYPH.match(subject or ""))


def build_subject(*, module: Module, policy: SubjectPolicy,
                  subject: str = "", entity_id: str | None = None,
                  event_label: str | None = None, status: str | None = None) -> str:
    """Compose the subject for the chosen policy. Idempotent — safe to call twice."""
    if policy is SubjectPolicy.VERBATIM:
        return subject or ""
    if policy is SubjectPolicy.ANCHOR:
        # Constant module glyph only. The subject string itself is untouched, so
        # Gmail's normalised subject is identical across the whole trail.
        base = subject or ""
        if _already_stamped(base):
            return base
        # "Re:" must stay at the very front or Gmail stops treating it as a reply.
        m = re.match(r"^\s*((?:[Rr][Ee]|[Ff][Ww][Dd]?):\s*)+", base)
        if m:
            prefix, rest = m.group(0), base[m.end():]
            return f"{prefix}{MODULES[module][1]} {rest}".rstrip()
        return f"{MODULES[module][1]} {base}".rstrip()

    glyph = _glyph_for(status, module)
    left = f"{glyph} {entity_id}".strip() if entity_id else glyph
    out = f"{left} — {event_label}".strip() if event_label else left
    return f"[{MAIL_ENV}] {out}" if MAIL_ENV != "PROD" else out


def stamp(msg: EmailMessage, *, module: Module,
          policy: SubjectPolicy = SubjectPolicy.EVENT,
          entity_type: str | None = None, entity_id: str | None = None,
          event: str | None = None, event_label: str | None = None,
          status: str | None = None, stage: str | None = None,
          actor: str | None = None, sender: str | None = None,
          system: str = "ERP") -> EmailMessage:
    """Apply the module's identity to an already-built message, in place.

    Deliberately does NOT touch Message-ID / In-Reply-To / References, nor the
    body — existing threading and the per-event banners keep working exactly as
    they do today.
    """
    addr = (sender or msg.get("From") or "").strip()
    # A From that already carries a display name is left alone.
    if addr and "<" not in addr:
        try:
            local, _, domain = addr.partition("@")
            if local and domain:
                del msg["From"]
                msg["From"] = Address(_display_name(module), local, domain)
        except Exception:                                     # noqa: BLE001
            pass                                              # never block a send

    subject = build_subject(
        module=module, policy=policy, subject=msg.get("Subject") or "",
        entity_id=entity_id, event_label=event_label, status=status,
    )
    del msg["Subject"]
    msg["Subject"] = subject

    for header, value in (
        ("List-Id", _list_id(module)),
        ("Auto-Submitted", "auto-generated"),
        ("X-Candor-System", system),
        ("X-Candor-Module", MODULES[module][0]),
        ("X-Candor-Entity-Type", entity_type),
        ("X-Candor-Entity-ID", str(entity_id) if entity_id is not None else None),
        ("X-Candor-Event", event),
        ("X-Candor-Status", status),
        ("X-Candor-Stage", stage),
        ("X-Candor-Actor", actor),
        ("X-Candor-Env", MAIL_ENV),
    ):
        if value:
            del msg[header]
            msg[header] = str(value)

    # Keeps standalone mails out of each other's threads. Never on ANCHOR mails,
    # where merging into one conversation is the whole point.
    if policy is SubjectPolicy.EVENT and entity_id:
        ref = f"{module.value}-{entity_id}-{(event or 'evt').lower()}"
        del msg["X-Entity-Ref-ID"]
        msg["X-Entity-Ref-ID"] = ref

    return msg

