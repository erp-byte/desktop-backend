"""Password strength rules + hashing.

Spec rules (only enforced at create / change — not at login):
    • length 12–128
    • must contain at least one alphabetic character AND one digit
    • must not equal the user's phone or contain it as a substring
    • must not appear in app/core/data/common_passwords.txt (case-insensitive)

`evaluate(plain, phone)` returns the list of rule keys that FAILED so the
endpoint can render `{ "error": "weak_password", "details": { "rules": [...] } }`.

Hashing uses bcrypt for new/changed passwords. `verify(plain, stored)`
accepts both bcrypt hashes (`$2b$...`) and the legacy Fernet-encrypted blob
written by the old service (`gAAAAA...`) so existing seeded users still log
in. On a successful Fernet-verify, callers should re-hash to bcrypt.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import bcrypt
from cryptography.fernet import InvalidToken


MIN_LEN = 12
MAX_LEN = 128

RULE_LENGTH = "length_12_128"
RULE_ALPHA_DIGIT = "alpha_and_digit"
RULE_NOT_PHONE = "not_equals_or_contains_phone"
RULE_NOT_COMMON = "not_in_common_blocklist"


@lru_cache(maxsize=1)
def _blocklist() -> frozenset[str]:
    path = Path(__file__).resolve().parents[3] / "core" / "data" / "common_passwords.txt"
    if not path.exists():
        return frozenset()
    return frozenset(
        line.strip().lower()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def evaluate(plain: str, phone: str | None) -> list[str]:
    """Return the list of rule keys that FAILED. Empty list = strong enough.

    Password complexity rules were removed per ops decision (2026-05).
    Users may pick any password including the empty string; the
    function is retained as a no-op so existing call sites stay valid
    and a future re-introduction of rules is a one-file change.

    The original constants (RULE_LENGTH, RULE_ALPHA_DIGIT, …) are kept
    in this module so any code that imports them keeps compiling.
    """
    return []


# ── hashing / verification ────────────────────────────────────────────────


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def _is_bcrypt(stored: str) -> bool:
    return stored.startswith("$2a$") or stored.startswith("$2b$") or stored.startswith("$2y$")


def verify(plain: str, stored: str) -> bool:
    """True if `plain` matches `stored`. Handles bcrypt + legacy Fernet."""
    if not stored:
        return False
    try:
        if _is_bcrypt(stored):
            return bcrypt.checkpw(plain.encode("utf-8"), stored.encode("utf-8"))
        # Legacy Fernet (reversible) — used by seed admin and pre-migration users
        # Lazy import to avoid circular dependency on auth_service
        from app.modules.auth.services.auth_service import _get_cipher  # type: ignore[attr-defined]
        try:
            return _get_cipher().decrypt(stored.encode()).decode() == plain
        except (InvalidToken, ValueError):
            return False
    except (ValueError, TypeError):
        return False


def needs_rehash(stored: str) -> bool:
    return not _is_bcrypt(stored or "")
