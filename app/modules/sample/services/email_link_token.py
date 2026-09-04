"""HMAC-signed tokens for the NPD email action magic-links.

The /email/npd-action (accept) and /email/promote-action (approve) links used
to authenticate solely on a caller-supplied `email` field — so anyone who knew
an approver's email plus the small integer request_id / dev_jc_id could POST an
approval and defeat the dual-approval gate. The links now carry a token bound to
the exact (action, id[, gate], email); the endpoint recomputes it with the
server secret and constant-time compares before trusting the email.

Bindings used by callers:
    accept      -> sign("npd", request_id, email)
    approve     -> sign("promote", dev_jc_id, approver_kind, email)
    req_cancel  -> sign("req_cancel", request_id, email)
    req_redate  -> sign("req_redate", request_id, email)

The reject link bounces through the web app (not backend-only), so it is not
signed here; rejecting is non-escalating (blocks a promotion, requires a reason).

The two dispatch-reminder links bounce through the web app like the reject link, but
unlike it they ARE signed: cancelling is terminal and irreversible, so an unsigned link
would let anyone who guessed an 8-digit request_id plus an address kill a live request.
"""

import hashlib
import hmac

from app.config import Settings


def _key() -> bytes:
    # Reuse the JWT signing secret (dev falls back to AUTH_ENCRYPTION_KEY, same
    # as jwt_service). Server-side only — never sent to the client.
    s = Settings()
    return (s.JWT_SECRET or s.AUTH_ENCRYPTION_KEY or "").encode("utf-8")


def sign(*parts: object) -> str:
    """Deterministic 32-char HMAC over the '|'-joined, trimmed, lower-cased parts.
    Lower-casing makes it tolerant of email/gate casing differences between the
    link-builder and the endpoint (both feed the same logical values)."""
    msg = "|".join(str(p).strip().lower() for p in parts).encode("utf-8")
    return hmac.new(_key(), msg, hashlib.sha256).hexdigest()[:32]


def verify(token: str | None, *parts: object) -> bool:
    """Constant-time check that `token` matches sign(*parts). Empty/None → False."""
    if not token:
        return False
    return hmac.compare_digest(token, sign(*parts))


if __name__ == "__main__":  # ponytail: HMAC round-trip self-check
    t = sign("promote", 123, "INV_MGR", "A@B.com")
    assert verify(t, "promote", 123, "inv_mgr", "a@b.com"), "case-insensitive round-trip"
    assert not verify(t, "promote", 124, "INV_MGR", "A@B.com"), "different id must fail"
    assert not verify(t, "npd", 123, "A@B.com"), "different action must fail"
    assert not verify("", "promote", 123, "INV_MGR", "A@B.com"), "empty token must fail"
    assert not verify("deadbeef", "promote", 123, "INV_MGR", "A@B.com"), "wrong token must fail"
    print("email_link_token self-check OK")
