"""Phone normalization for the Indian login flow.

Spec: accept either E.164 (+919876543210) or raw 10-digit Indian (9876543210).
We normalize to E.164 internally — `normalize("9876543210") == "+919876543210"`.

For backward compatibility, `lookup_keys()` returns the candidate phone
strings to query the DB with, so users created before the normalizer existed
(stored as bare 10 digits, e.g. seed admin "9004464207") still match.
"""

from __future__ import annotations

import re

_INDIA_CC = "+91"
_DIGITS = re.compile(r"\D+")


def normalize(raw: str | None) -> str | None:
    """Best-effort normalize to E.164. Returns None for unrecognised input."""
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    # already E.164?
    if s.startswith("+") and s[1:].isdigit() and 8 <= len(s[1:]) <= 15:
        return s
    digits = _DIGITS.sub("", s)
    if not digits:
        return None
    # 10-digit Indian mobile (starts with 6/7/8/9)
    if len(digits) == 10 and digits[0] in "6789":
        return f"{_INDIA_CC}{digits}"
    # 11-digit "0" prefix Indian mobile
    if len(digits) == 11 and digits[0] == "0" and digits[1] in "6789":
        return f"{_INDIA_CC}{digits[1:]}"
    # 12-digit "91" prefix Indian mobile
    if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
        return f"{_INDIA_CC}{digits[2:]}"
    # generic international: 8–15 digits → just slap a + on it
    if 8 <= len(digits) <= 15:
        return f"+{digits}"
    return None


def lookup_keys(raw: str | None) -> list[str]:
    """Return DB lookup candidates: normalized E.164 + bare-10-digit fallback."""
    out: list[str] = []
    n = normalize(raw)
    if n:
        out.append(n)
        if n.startswith(_INDIA_CC):
            out.append(n[len(_INDIA_CC):])  # legacy 10-digit row
    if raw and raw.strip() and raw.strip() not in out:
        out.append(raw.strip())  # raw user input as last resort
    return out
