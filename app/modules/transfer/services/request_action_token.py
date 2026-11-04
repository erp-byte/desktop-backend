"""Signed links for deciding a transfer request from its notice email.

HS256 JWT on the app's JWT signing secret (auth.jwt_service), with its own ``type``
claim so a link can never be swapped for a sign-in token or a customer-return link
(customer_returns/approval_token.py is built the same way). The token names the
request, the action and the ERP user the email was sent to; it is the whole
authorization for the link, so it expires (TTL_DAYS).

A token is not single-use by itself; the decision is. Once the request has moved
past the step, every link to that step (this one and the copies other people got)
only reports the state it is in (request_action.decide).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt as pyjwt

from app.modules.auth.services.jwt_service import _alg, _secret

TOKEN_TYPE = "transfer_request_action"
TTL_DAYS = 14
# The decisions, named as request_decision_service names them (store, then manager).
ACTIONS = ("accept", "store_hold", "store_reject", "approve", "hold", "reject")


def make_token(request_id: int, action: str, user_id: int, email: str) -> str:
    if action not in ACTIONS:
        raise ValueError(f"unknown transfer request action {action!r}")
    now = datetime.now(timezone.utc)
    payload = {
        "type": TOKEN_TYPE,
        "rid": int(request_id),
        "act": action,
        "uid": int(user_id),                    # the user the email was sent to
        "em": (email or "").strip().lower(),    # ...at this address
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=TTL_DAYS)).timestamp()),
    }
    return pyjwt.encode(payload, _secret(), algorithm=_alg())


def verify_token(token: Optional[str]) -> Optional[dict]:
    """The claims of a valid link token, else None (expired, tampered, another kind
    of token, or claims of the wrong shape)."""
    if not token:
        return None
    try:
        claims = pyjwt.decode(token, _secret(), algorithms=[_alg()])
    except pyjwt.InvalidTokenError:
        return None
    if claims.get("type") != TOKEN_TYPE or claims.get("act") not in ACTIONS:
        return None
    if not all(isinstance(claims.get(k), int) and not isinstance(claims.get(k), bool) for k in ("rid", "uid")):
        return None
    if not claims.get("em"):
        return None
    return claims
