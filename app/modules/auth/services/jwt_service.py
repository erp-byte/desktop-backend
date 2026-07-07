"""JWT issuance and verification for access + refresh tokens.

Tokens are signed HS256 with `Settings.JWT_SECRET`. In dev *only*, if
JWT_SECRET is unset we derive one from AUTH_ENCRYPTION_KEY so local startup
doesn't break. Outside dev (`APP_ENV != "dev"`), Settings refuses to boot
without an explicit JWT_SECRET — see `_require_jwt_secret_in_prod`.

Access-token claims:
    sub                 user_id (str)
    jti                 unique uuid (per access token)
    parent_jti          jti of the refresh token that minted this access
    type                "access"
    iat / exp           seconds since epoch
    iss                 settings.JWT_ISSUER
    is_admin            bool (cached; permission re-check still happens at scope)
    must_change_password bool

Refresh-token claims:
    sub                 user_id (str)
    jti                 unique uuid
    parent_jti          previous refresh's jti (None for login-issued)
    chain_root          original login-issued jti (== jti for the first one)
    type                "refresh"
    iat / exp           seconds since epoch
    iss                 settings.JWT_ISSUER

Verification raises `AuthError` with the precise spec error code.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt as pyjwt

from app.config import Settings
from app.core.middleware.request_context import AuthError

logger = logging.getLogger(__name__)


# ── secret resolution ─────────────────────────────────────────────────────


def _secret(settings: Settings | None = None) -> str:
    """Resolve the JWT signing secret. Refuses dev fallback in non-dev env."""
    s = settings or Settings()
    if s.JWT_SECRET:
        return s.JWT_SECRET
    # CR-02: never fall back to AUTH_ENCRYPTION_KEY-derived secret outside dev.
    # Settings._require_jwt_secret_in_prod already prevents this code path
    # from being reached when APP_ENV != 'dev', but defend in depth in case
    # Settings is bypassed (tests, scripts).
    if s.APP_ENV != "dev":
        raise RuntimeError(
            "JWT_SECRET is required when APP_ENV != 'dev'. "
            "Refusing to derive a signing key from AUTH_ENCRYPTION_KEY."
        )
    fallback = s.AUTH_ENCRYPTION_KEY or os.environ.get("AUTH_ENCRYPTION_KEY", "")
    if fallback:
        logger.warning(
            "JWT_SECRET unset — using derived dev fallback. DO NOT deploy this way."
        )
        return f"dev-jwt::{fallback}"
    raise RuntimeError(
        "JWT_SECRET is not set and no AUTH_ENCRYPTION_KEY for dev fallback. "
        "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(64))\""
    )


def _alg(settings: Settings | None = None) -> str:
    return (settings or Settings()).JWT_ALGORITHM


def _iss(settings: Settings | None = None) -> str:
    return (settings or Settings()).JWT_ISSUER


# ── token issuance ────────────────────────────────────────────────────────


def issue_access_token(
    *,
    user_id: int | str,
    parent_jti: str,
    is_admin: bool,
    must_change_password: bool,
    settings: Settings | None = None,
) -> tuple[str, str, int]:
    """Mint a fresh access JWT. Returns (jwt, jti, expires_in_seconds)."""
    s = settings or Settings()
    now = datetime.now(timezone.utc)
    exp_dt = now + timedelta(seconds=s.ACCESS_TOKEN_TTL_SECONDS)
    jti = str(uuid.uuid4())
    payload = {
        "sub": str(user_id),
        "jti": jti,
        "parent_jti": parent_jti,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int(exp_dt.timestamp()),
        "iss": _iss(s),
        "is_admin": bool(is_admin),
        "must_change_password": bool(must_change_password),
    }
    token = pyjwt.encode(payload, _secret(s), algorithm=_alg(s))
    return token, jti, s.ACCESS_TOKEN_TTL_SECONDS


def issue_refresh_token(
    *,
    user_id: int | str,
    parent_jti: str | None,
    chain_root: str | None,
    settings: Settings | None = None,
) -> tuple[str, str, str, datetime, int]:
    """Mint a fresh refresh JWT.

    Returns (jwt, jti, chain_root, expires_at_utc, expires_in_seconds).
    chain_root defaults to the new jti on initial login (parent_jti=None).
    """
    s = settings or Settings()
    now = datetime.now(timezone.utc)
    exp_dt = now + timedelta(seconds=s.REFRESH_TOKEN_TTL_SECONDS)
    jti = str(uuid.uuid4())
    root = chain_root or jti
    payload = {
        "sub": str(user_id),
        "jti": jti,
        "parent_jti": parent_jti,
        "chain_root": root,
        "type": "refresh",
        "iat": int(now.timestamp()),
        "exp": int(exp_dt.timestamp()),
        "iss": _iss(s),
    }
    token = pyjwt.encode(payload, _secret(s), algorithm=_alg(s))
    return token, jti, root, exp_dt, s.REFRESH_TOKEN_TTL_SECONDS


# ── verification ──────────────────────────────────────────────────────────


def _decode(token: str, expected_type: str) -> dict[str, Any]:
    try:
        payload = pyjwt.decode(
            token,
            _secret(),
            algorithms=[_alg()],
            issuer=_iss(),
            # MD-05: require `iss` explicitly so a token missing the claim is
            # rejected by pyjwt with InvalidIssuerError, not silently accepted.
            options={"require": ["exp", "iat", "sub", "jti", "type", "iss"]},
        )
    except pyjwt.ExpiredSignatureError:
        code = "token_expired" if expected_type == "refresh" else "invalid_access_token"
        raise AuthError(
            code=code,
            message="Token has expired",
            status_code=401,
        )
    except pyjwt.InvalidTokenError as e:
        code = "invalid_refresh_token" if expected_type == "refresh" else "invalid_access_token"
        raise AuthError(
            code=code,
            message=f"Invalid token: {e.__class__.__name__}",
            status_code=401,
        )

    if payload.get("type") != expected_type:
        code = "invalid_refresh_token" if expected_type == "refresh" else "invalid_access_token"
        raise AuthError(code=code, message="Wrong token type", status_code=401)
    return payload


def verify_access(token: str) -> dict[str, Any]:
    return _decode(token, expected_type="access")


def verify_refresh(token: str) -> dict[str, Any]:
    return _decode(token, expected_type="refresh")
