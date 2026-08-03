"""Which FORM an existing product is sampled in, and whether that form needs production.

The general sample module samples products that ALREADY EXIST. Nothing here develops a new
product — that is NPD, a separate module with its own requisition → job card → dispatch
flow, and a general sample never crosses into it.

The form comes from ``all_sku.sale_group``, the master classifier the rest of the ERP
already uses (Customer Returns auto-fills the same field on item pick). Values in the live
master, lower-cased, are:

    va · rpc · bulk · dates rpc · dates bulk · dates ing · pm · wip · NULL

``dates`` is a product family prefix, not a form — "dates rpc" is repacked, the same as
"rpc". Stripping it is what lets one rule cover both.

Routing, per the module owner:
    BULK → issued directly from existing stock; no job card, straight to gate pass / DC.
    VA   → value added, so it has to be made: raise a plan/job card, tell production.
    RPC  → repacked, likewise a job card.

A form this module cannot recognise resolves to ``None`` and is NEVER routed by guesswork:
picking wrong either wastes a production run or skips one the sample needed, and 105 FGs in
the master carry no sale_group at all.
"""
from __future__ import annotations

import re

BULK = "BULK"
VA = "VA"
RPC = "RPC"
ING = "ING"

#: Forms that require the article to be produced/packed before it can be sampled.
NEEDS_JOB_CARD = frozenset({VA, RPC})

#: Human labels for the UI and the mail trail.
LABEL = {
    BULK: "Bulk (direct)",
    VA: "Value added (VA)",
    RPC: "Repacked (RPC)",
    ING: "Ingredient",
}

# Base sale_group token -> form. The `dates ` family prefix is stripped before lookup.
_FORMS = {
    "bulk": BULK,
    "va": VA,
    "rpc": RPC,
    "ing": ING,
}

_WS = re.compile(r"\s+")
_DATES_PREFIX = re.compile(r"^dates\s+")


def normalise(sale_group) -> str:
    """Lower-cased, whitespace-collapsed sale_group with the `dates` family prefix removed.
    Returns "" for NULL/blank so callers get one empty sentinel rather than None vs ''."""
    s = _WS.sub(" ", str(sale_group or "").strip().lower())
    return _DATES_PREFIX.sub("", s)


def classify(sale_group) -> str | None:
    """The sample form for an article's sale_group, or None when it cannot be determined.

    None is a real answer, not a failure: `pm`, `wip`, `0` and NULL all reach it, and the
    caller must ask a human rather than assume a route."""
    return _FORMS.get(normalise(sale_group))


def needs_job_card(form) -> bool:
    """True when this form has to be produced or packed before the sample can be issued.

    Unknown forms are False — an unrecognised article must never silently trigger a
    production run. The caller is expected to have blocked on classify() returning None
    before it ever gets here."""
    return form in NEEDS_JOB_CARD


def label(form) -> str:
    return LABEL.get(form, "Unclassified")
