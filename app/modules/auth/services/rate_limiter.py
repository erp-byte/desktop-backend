"""Process-local rate limiter for login attempts.

Limits per (ip, phone) tuple. The key is intentionally narrow so that one
attacker cannot lock out a victim by spamming from one IP — we only block
*that ip*'s attempts on *that phone*.

NOTE: Single-process / in-memory. On Lambda this resets per cold start; on a
multi-worker uvicorn it's per-worker. For real per-user enforcement across
the cluster, swap `_HITS` for a Redis SETEX/INCR pair behind the same
`check_and_record` API.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque

from app.config import Settings
from app.core.middleware.request_context import AuthError


_HITS: dict[tuple[str, str], Deque[float]] = {}
_LOCK = threading.Lock()


# Fallback defaults that match app.config.Settings — used only when the
# caller didn't pass a Settings instance AND we can't construct one
# (e.g. CWD lost track of the `.env` file mid-request). Keeps login
# semantics consistent rather than 500-ing on a config-load edge case.
_FALLBACK_WINDOW_SECONDS = 60
_FALLBACK_MAX = 10


def check_and_record(
    ip: str | None,
    phone: str | None,
    settings: Settings | None = None,
) -> None:
    """Record one attempt; raise 429 AuthError if over the limit.

    Includes a `Retry-After` header (seconds until the oldest hit ages out).

    Caller should pass `settings=request.app.state.settings` so we reuse
    the lifespan-cached config. If `settings` is None we fall back to
    constructing a fresh Settings() (which itself falls back to module
    defaults when the env file can't be resolved).
    """
    if settings is None:
        try:
            settings = Settings()
        except Exception:
            # Config load failed at request time — fall back to defaults
            # rather than 500'ing the login. The lifespan would have
            # crashed earlier if config were truly absent, so this branch
            # only ever fires on a CWD / env-file-resolution edge case.
            window = _FALLBACK_WINDOW_SECONDS
            cap = _FALLBACK_MAX
            settings = None  # type: ignore[assignment]

    if settings is not None:
        window = settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS
        cap = settings.LOGIN_RATE_LIMIT_MAX
    key = ((ip or "?"), (phone or "?"))
    now = time.monotonic()
    cutoff = now - window

    with _LOCK:
        dq = _HITS.setdefault(key, deque())
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= cap:
            retry_after = max(1, int(window - (now - dq[0])))
            raise AuthError(
                code="rate_limit_exceeded",
                message="Too many login attempts. Try again later.",
                status_code=429,
                details={
                    "retry_after_seconds": retry_after,
                    "limit": cap,
                    "window_seconds": window,
                },
                headers={"Retry-After": str(retry_after)},
            )
        dq.append(now)


def reset(ip: str | None, phone: str | None) -> None:
    """Clear the bucket on successful login so the user isn't penalised next time."""
    key = ((ip or "?"), (phone or "?"))
    with _LOCK:
        _HITS.pop(key, None)
