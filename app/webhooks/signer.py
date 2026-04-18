"""HMAC-SHA256 webhook payload signing."""

import hashlib
import hmac


def sign_payload(secret: str, body: str) -> str:
    """Return 'sha256=<hex>' signature for webhook verification."""
    return "sha256=" + hmac.new(
        secret.encode(), body.encode(), hashlib.sha256
    ).hexdigest()


def verify_signature(secret: str, body: str, signature: str) -> bool:
    """Constant-time comparison of expected vs provided signature."""
    expected = sign_payload(secret, body)
    return hmac.compare_digest(expected, signature)
