"""Storage abstraction for receipt-side document uploads.

Two backends behind a common contract:

    LocalStorage  — writes under STORAGE_LOCAL_BASE_DIR/<key>; presigned URL
                    is a JWT-bearer-protected `/api/v1/receipt/files/<token>`
                    endpoint (the spec's "5-minute TTL" is honoured via the
                    JWT exp claim).
    S3Storage     — writes to the configured bucket with SSE; presigned URL
                    is a real boto3 presign with the requested TTL.

Plus pure helpers:
    sniff_mime(bytes, declared) → (effective_mime, mismatch_bool)
                  Reads magic bytes for PDF / JPEG / PNG only; returns the
                  declared type when the format isn't in the allowlist (the
                  router should have already rejected it via 415).
    new_storage_key(prefix, ext) → "<prefix>/<YYYY/MM/DD>/<uuid><ext>"
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import jwt as pyjwt

from app.config import Settings

logger = logging.getLogger(__name__)


ALLOWED_RECEIPT_MIMES: frozenset[str] = frozenset({
    "application/pdf",
    "image/jpeg",
    "image/png",
})

_EXT_FOR_MIME: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}


# ── magic-byte sniffer ───────────────────────────────────────────────────


def sniff_mime(content: bytes, declared: str | None) -> tuple[str, bool]:
    """Return (effective_mime, mismatch). `mismatch` is True when the
    sniffed type differs from `declared` (router 415s on True).

    WR-01: when content does NOT match a known magic-byte signature, we
    return `application/octet-stream` (NOT the declared type) so the caller's
    allowlist check at the call site rejects it. This closes the bypass
    where an attacker uploads a ZIP/HTML/EXE labelled application/pdf and
    has it stored + later served as PDF.
    """
    if not content:
        # Empty body — let the empty-file check at the call site handle it.
        return "application/octet-stream", False

    sniffed: str | None = None
    if content.startswith(b"%PDF-"):
        sniffed = "application/pdf"
    elif content.startswith(b"\xff\xd8\xff"):
        sniffed = "image/jpeg"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        sniffed = "image/png"

    if sniffed is None:
        # Refuse to second-guess unfamiliar content. The caller compares
        # this against ALLOWED_RECEIPT_MIMES and 415s.
        return "application/octet-stream", False

    declared_norm = (declared or "").lower().split(";", 1)[0].strip()
    mismatch = bool(declared_norm) and declared_norm != sniffed
    return sniffed, mismatch


def new_storage_key(prefix: str, mime_type: str) -> str:
    now = datetime.now(timezone.utc)
    ext = _EXT_FOR_MIME.get(mime_type, "")
    return f"{prefix}/{now:%Y/%m/%d}/{uuid.uuid4()}{ext}"


# ── presigned-URL token (LocalStorage) ───────────────────────────────────


def _local_token(s3_key: str, ttl_seconds: int, settings: Settings) -> str:
    payload = {
        "sub": s3_key,
        "exp": int(time.time()) + ttl_seconds,
        "iss": settings.JWT_ISSUER,
        "purpose": "receipt-file-download",
    }
    # Reuse the JWT secret resolution logic from jwt_service so the dev
    # fallback works the same way.
    from app.modules.auth.services.jwt_service import _secret, _alg
    return pyjwt.encode(payload, _secret(settings), algorithm=_alg(settings))


def verify_local_token(token: str, settings: Settings) -> str | None:
    """Verify a local-storage download token. Returns the s3_key on success.

    WR-02: `iss` is in the require list — pyjwt only enforces issuer when the
    claim is present, so requiring it explicitly closes a token-replay
    surface where another consumer of the same JWT secret could mint a
    purpose-tagged token without an iss claim.
    """
    from app.modules.auth.services.jwt_service import _secret, _alg
    try:
        payload = pyjwt.decode(
            token,
            _secret(settings),
            algorithms=[_alg(settings)],
            issuer=settings.JWT_ISSUER,
            options={"require": ["exp", "sub", "purpose", "iss"]},
        )
    except pyjwt.InvalidTokenError:
        return None
    if payload.get("purpose") != "receipt-file-download":
        return None
    return payload.get("sub")


# ── backends ─────────────────────────────────────────────────────────────


class LocalStorage:
    def __init__(self, base_dir: str):
        self.base = Path(base_dir).resolve()
        self.base.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, key: str) -> Path:
        """WR-07: resolve `key` to an absolute path INSIDE `self.base` and
        refuse to follow symlinks out. Uses `Path.relative_to` which raises
        ValueError on escape (no fragile `startswith` on stem-collisions
        like 'C:\\storage' vs 'C:\\storageX'). Symlink-aware via strict=False
        + post-resolve relative_to check.
        """
        # Reject obviously-bogus keys early
        if not key or "\x00" in key:
            raise PermissionError("invalid key")
        candidate = (self.base / key).resolve(strict=False)
        try:
            candidate.relative_to(self.base)
        except ValueError:
            raise PermissionError(f"path escape: {key!r}")
        # If it exists and is a symlink pointing outside, reject.
        if candidate.is_symlink():
            target = candidate.resolve(strict=True)
            try:
                target.relative_to(self.base)
            except ValueError:
                raise PermissionError("symlink target outside base")
        return candidate

    def put(self, key: str, content: bytes, mime_type: str) -> str:
        path = self._safe_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return key

    def open_for_read(self, key: str) -> bytes:
        path = self._safe_path(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        return path.read_bytes()

    def delete(self, key: str) -> None:
        try:
            path = self._safe_path(key)
        except PermissionError:
            return  # cleanup is best-effort; never raise
        if path.is_file():
            path.unlink()

    def presigned_get(self, key: str, ttl_seconds: int, settings: Settings) -> str:
        token = _local_token(key, ttl_seconds, settings)
        return f"/api/v1/receipt/files/{token}"


class S3Storage:
    """Lazy boto3 client; only instantiated when STORAGE_BACKEND='s3'."""

    def __init__(self, bucket: str, region: str | None = None,
                 sse: str = "AES256"):
        import boto3
        self._bucket = bucket
        self._sse = sse
        kwargs: dict = {}
        if region:
            kwargs["region_name"] = region
        self._client = boto3.client("s3", **kwargs)

    def put(self, key: str, content: bytes, mime_type: str) -> str:
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ContentType=mime_type,
                ServerSideEncryption=self._sse,
            )
        except Exception as e:
            logger.error("s3.put_failed key=%s err=%r", key, e)
            raise
        return key

    def open_for_read(self, key: str) -> bytes:
        obj = self._client.get_object(Bucket=self._bucket, Key=key)
        return obj["Body"].read()

    def presigned_get(self, key: str, ttl_seconds: int, settings: Settings) -> str:
        try:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=ttl_seconds,
            )
        except Exception as e:
            logger.error("s3.presign_failed key=%s err=%r", key, e)
            return ""  # router shows null/empty; client must retry


# ── factory ──────────────────────────────────────────────────────────────


_BACKEND_SINGLETON: object | None = None


def get_storage(settings: Settings):
    global _BACKEND_SINGLETON
    if _BACKEND_SINGLETON is not None:
        return _BACKEND_SINGLETON
    backend = (settings.STORAGE_BACKEND or "local").lower()
    if backend == "s3":
        bucket = os.environ.get("RECEIPT_S3_BUCKET", "")
        region = os.environ.get("AWS_DEFAULT_REGION") or None
        if not bucket:
            raise RuntimeError(
                "STORAGE_BACKEND='s3' but RECEIPT_S3_BUCKET env var is not set."
            )
        _BACKEND_SINGLETON = S3Storage(bucket=bucket, region=region)
    else:
        _BACKEND_SINGLETON = LocalStorage(settings.STORAGE_LOCAL_BASE_DIR or "./storage")
    return _BACKEND_SINGLETON
