"""Request-context middleware: X-Request-ID, security headers, structured error envelope.

Every response (success or error) carries:
    X-Request-ID:           uuid4
    Cache-Control:          no-store
    X-Content-Type-Options: nosniff

Every non-2xx response carries the documented JSON envelope:
    {
      "error":      "<machine code>",
      "message":    "<human-readable>",
      "request_id": "<uuid>",
      "timestamp":  "<ISO8601 Z>",
      "details":    {}
    }

Routes raise `AuthError` (or any `HTTPException`) and the handler renders the
envelope. Non-HTTP exceptions render as 500 `internal_error`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


# ── public exception type ─────────────────────────────────────────────────


class AuthError(Exception):
    """Domain error rendered through the structured envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        self.headers = headers or {}
        super().__init__(message)


# ── header + request-id middleware ────────────────────────────────────────


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Stamp X-Request-ID + security headers on every response."""

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = rid

        try:
            response = await call_next(request)
        except Exception:
            # Last-resort: handlers below should have already rendered the envelope.
            logger.exception("Unhandled exception (request_id=%s)", rid)
            response = JSONResponse(
                status_code=500,
                content=_envelope("internal_error", "Internal server error", rid),
            )

        response.headers["X-Request-ID"] = rid
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response


# ── envelope helpers ──────────────────────────────────────────────────────


def _now_iso() -> str:
    """MD-01: single datetime.now() so the seconds and milliseconds can't
    disagree at a tick boundary."""
    n = datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"


def _envelope(code: str, message: str, request_id: str, details: dict | None = None) -> dict:
    return {
        "error": code,
        "message": message,
        "request_id": request_id,
        "timestamp": _now_iso(),
        "details": details or {},
    }


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or str(uuid.uuid4())


# ── exception handlers ────────────────────────────────────────────────────


_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}


def _error_headers(rid: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """MD-04: defensively include security headers on every error response so
    they survive even if RequestContextMiddleware is removed/reordered.
    LO-01: X-Request-ID is set LAST so caller-supplied headers can't override it.
    """
    out: dict[str, str] = {**_SECURITY_HEADERS, **(extra or {}), "X-Request-ID": rid}
    return out


async def auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
    rid = _request_id(request)
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(exc.code, exc.message, rid, exc.details),
        headers=_error_headers(rid, exc.headers),
    )


_DEFAULT_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    408: "request_timeout",   # NI-05
    409: "conflict",
    415: "unsupported_media_type",
    422: "validation_error",
    423: "account_locked",
    429: "rate_limit_exceeded",
    500: "internal_error",
    502: "bad_gateway",
    503: "service_unavailable",
    504: "gateway_timeout",
}


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    rid = _request_id(request)
    detail = exc.detail
    if isinstance(detail, dict):
        # callers can raise HTTPException(detail={"error": "...", "message": "...", "details": {...}})
        code = detail.get("error") or _DEFAULT_HTTP_CODES.get(exc.status_code, "error")
        # When the caller supplied an explicit error code but no message,
        # use the code as a fallback (e.g. "invalid_uom") rather than the
        # generic status name ("Bad request"). The frontend's
        # friendlyApiError catalog can then map the code to an actual
        # sentence; an operator should never see just "Bad request".
        # `details` (with the dict the caller passed minus the envelope
        # keys) is still surfaced so the frontend has all the context.
        explicit_message = detail.get("message")
        if explicit_message:
            message = explicit_message
        elif detail.get("error"):
            # Humanise the code for the rare case where the frontend's
            # catalog doesn't have it either: "invalid_uom" → "Invalid uom"
            message = detail["error"].replace("_", " ").capitalize()
        else:
            message = _http_status_message(exc.status_code)
        details = detail.get("details") or {
            k: v for k, v in detail.items()
            if k not in ("error", "message", "details")
        }
    else:
        code = _DEFAULT_HTTP_CODES.get(exc.status_code, "error")
        message = str(detail) if detail else _http_status_message(exc.status_code)
        details = {}

    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(code, message, rid, details),
        headers=_error_headers(rid, dict(exc.headers or {})),
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    rid = _request_id(request)
    errors = exc.errors()
    logger.warning(
        "422 validation failed: %s %s (request_id=%s) errors=%s body=%r",
        request.method,
        request.url.path,
        rid,
        errors,
        getattr(exc, "body", None),
    )
    return JSONResponse(
        status_code=422,
        content=_envelope(
            "validation_error",
            "Request body failed validation",
            rid,
            {"errors": errors},
        ),
        headers=_error_headers(rid),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    rid = _request_id(request)
    logger.exception("Unhandled exception (request_id=%s)", rid)
    return JSONResponse(
        status_code=500,
        content=_envelope("internal_error", "Internal server error", rid),
        headers=_error_headers(rid),
    )


def _http_status_message(code: int) -> str:
    return {
        400: "Bad request",
        401: "Authentication required",
        403: "Forbidden",
        404: "Not found",
        405: "Method not allowed",
        409: "Conflict",
        415: "Unsupported media type",
        422: "Validation failed",
        423: "Account locked",
        429: "Too many requests",
        500: "Internal server error",
    }.get(code, f"HTTP {code}")


# ── one-shot installer ────────────────────────────────────────────────────


def install(app: FastAPI) -> None:
    """Mount middleware + register exception handlers in the right order."""
    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(AuthError, auth_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
