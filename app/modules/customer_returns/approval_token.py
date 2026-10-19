"""Signed magic-link tokens for the Customer-Returns email Approve/Reject/Hold
buttons.

HS256 JWT reusing the app's JWT signing secret (via auth.jwt_service), with a
dedicated ``type`` claim so an approval link can never be swapped for an auth
token (or vice-versa). Default 14-day TTL. The token says WHO the link was minted
for (``approver``, an e-mail) and what it does; it is signed + expiring (unlike the
legacy live tokenless ``/rtv/action`` scheme, which trusted an unsigned bh_email
query). It is not the whole authorization: the click opens a confirmation page
(GET /email-action changes nothing) and the confirm POST decides as that e-mail
through approval_service.apply_cr_action, which re-checks at that moment that the
address is still one of the CR's approvers — the same rule as in-app and WhatsApp.
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
    wrong type / bad action / no approver to decide as)."""
    if not token:
        return None
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
    # The decision is taken as this address, so a link without one decides nothing.
    if not str(claims.get("approver") or "").strip():
        return None
    return claims
