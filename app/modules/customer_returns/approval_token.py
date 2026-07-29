"""Signed magic-link tokens for the Customer-Returns email Approve/Reject/Hold
buttons.

HS256 JWT reusing the app's JWT signing secret (via auth.jwt_service), with a
dedicated ``type`` claim so an approval link can never be swapped for an auth
token (or vice-versa). Default 14-day TTL. The token is the whole authorization
for the emailed one-click action — it is signed + expiring (unlike the legacy
live tokenless ``/rtv/action`` scheme, which trusted an unsigned bh_email query).
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

import jwt as pyjwt

from app.config import Settings
from app.modules.auth.services.jwt_service import _secret, _alg

_TOKEN_TYPE = "cr_action"
_VALID_ACTIONS = ("approve", "reject", "hold")


def make_action_token(company: str, cr_id: str, action: str, approver_email: str) -> str:
    s = Settings()
    now = datetime.now(timezone.utc)
    ttl_days = getattr(s, "CR_ACTION_TOKEN_TTL_DAYS", 14) or 14
    payload = {
        "type": _TOKEN_TYPE,
        "company": company,
        "cr_id": cr_id,
        "action": action,            # approve | reject | hold
        "approver": approver_email,  # BH the link was minted for (audit actor)
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=ttl_days)).timestamp()),
    }
    return pyjwt.encode(payload, _secret(s), algorithm=_alg(s))


def verify_action_token(token: str) -> Optional[dict]:
    """Claims for a valid CR-action token, else None (expired / tampered /
    wrong type / bad action)."""
    try:
        claims = pyjwt.decode(token, _secret(), algorithms=[_alg()])
    except pyjwt.InvalidTokenError:
        return None
    if claims.get("type") != _TOKEN_TYPE:
        return None
    if claims.get("action") not in _VALID_ACTIONS:
        return None
    if not claims.get("cr_id") or not claims.get("company"):
        return None
    return claims
