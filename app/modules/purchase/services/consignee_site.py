"""Which plant a PO is for, read off the start of its consignee address.

Tally's consignee address opens with the plant code -- "W-202(A), MIDC,, ...",
"A-185 , MIDC ...", "A-68 , Plot No-68,, ..." -- and once as "Plot No. W202A,
...". The code is one letter and a number, optionally after "Plot No.". Its
site key (upper case, punctuation dropped: "W202", "A185", "A68") is compared
against the scope keys of the sites asked for, so every spelling of a site in
the shared site table counts.

The same rule exists twice: CONSIGNEE_SITE_KEY_SQL for WHERE clauses and
consignee_site() for the API projection. tests/services/test_po_consignee_site.py
keeps the two in step.
"""

from __future__ import annotations

import re

from app.core.sites import canonical_site, site_key, site_kind

_PATTERN = r"^\s*(?:plot\s*no\.?\s*)?([a-z]\s*-?\s*[0-9]+)"
_RE = re.compile(_PATTERN, re.IGNORECASE)

# Site key of po_header.consignee_address, NULL when it opens with no plant code.
CONSIGNEE_SITE_KEY_SQL = (
    "upper(regexp_replace((regexp_match(consignee_address, "
    f"'{_PATTERN}', 'i'))[1], '[^A-Za-z0-9]', '', 'g'))"
)


def consignee_site_key(address: str | None) -> str | None:
    """'W202' for 'W-202(A), MIDC,, ...'; None when no plant code opens it."""
    m = _RE.match(address or "")
    return site_key(m.group(1)) if m else None


def consignee_site(address: str | None) -> str | None:
    """The canonical site the address is for ('W202', 'A185', 'A68'), or None
    when it opens with no plant code or with one the site table doesn't know."""
    key = consignee_site_key(address)
    if not key or site_kind(key) == "unknown":
        return None
    return canonical_site(key)
