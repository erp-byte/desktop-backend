"""S3-only storage helper for vendor document uploads.

Vendor docs are S3 only — no local filesystem fallback. Uploads stay in
memory (the router reads the multipart body into bytes, sniffs MIME,
hands the bytes to `put()` which streams them straight to S3 via
boto3, then drops the reference). Nothing ever lands on the host disk.

That means:
    • runs cleanly on a read-only filesystem (Lambda, container)
    • no temp-file cleanup
    • no path-traversal surface (no local paths to begin with)
    • requires VENDOR_S3_BUCKET to be set — failure to upload is loud
      (`RuntimeError`, surfaced as HTTP 503 by the router)

Stored URL shape: `s3://{bucket}/{key}`. The bucket is public-blocked;
a presign step is needed for browser download — see `presigned_get`.

Key layout matches the schema-map convention:
    vendors/{supplier_code}/{doc_type}/{yyyy}/{uuid}_{name}.{ext}

MIME allowlist: PDF, JPEG, PNG. Max 25 MB.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


ALLOWED_VENDOR_MIMES: frozenset[str] = frozenset({
    "application/pdf",
    "image/jpeg",
    "image/png",
})

_EXT_FOR_MIME: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}

MAX_VENDOR_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB hard cap


# ── magic-byte sniffer ───────────────────────────────────────────────────


def sniff_mime(content: bytes, declared: str | None) -> tuple[str, bool]:
    """Return (effective_mime, mismatch). `mismatch=True` ⇒ router must 415."""
    if not content:
        return "application/octet-stream", False
    sniffed: str | None = None
    if content.startswith(b"%PDF-"):
        sniffed = "application/pdf"
    elif content.startswith(b"\xff\xd8\xff"):
        sniffed = "image/jpeg"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        sniffed = "image/png"
    if sniffed is None:
        return "application/octet-stream", False
    declared_norm = (declared or "").lower().split(";", 1)[0].strip()
    mismatch = bool(declared_norm) and declared_norm != sniffed
    return sniffed, mismatch


def new_vendor_key(
    supplier_code: str,
    doc_type: str,
    mime_type: str,
    original_filename: str | None = None,
) -> str:
    """vendors/{supplier_code}/{doc_type}/{yyyy}/{uuid}_{name}.{ext}"""
    now = datetime.now(timezone.utc)
    ext = _EXT_FOR_MIME.get(mime_type, "")
    safe_name = ""
    if original_filename:
        safe_name = Path(original_filename).stem
        safe_name = "".join(c for c in safe_name if c.isalnum() or c in ("-", "_"))[:48]
        if safe_name:
            safe_name = "_" + safe_name
    safe_code = "".join(c for c in supplier_code if c.isalnum() or c in ("-", "_"))[:32] or "unknown"
    safe_type = "".join(c for c in doc_type if c.isalnum() or c in ("-", "_"))[:32] or "OTHER"
    return f"vendors/{safe_code}/{safe_type}/{now:%Y}/{uuid.uuid4()}{safe_name}{ext}"


# ── S3 backend ───────────────────────────────────────────────────────────


class _S3VendorStorage:
    def __init__(
        self,
        bucket: str,
        region: str | None = None,
        sse: str = "AES256",
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        aws_session_token: str | None = None,
    ):
        import boto3  # local import keeps boto3 optional at module-import time

        self._bucket = bucket
        self._region = region
        self._sse = sse
        kwargs: dict = {}
        if region:
            kwargs["region_name"] = region
        # Pass explicit credentials only when present. When the caller
        # leaves them None, boto3's default chain runs (env vars, ~/.aws,
        # IAM role) — that's the right behaviour for prod / Lambda.
        if aws_access_key_id and aws_secret_access_key:
            kwargs["aws_access_key_id"] = aws_access_key_id
            kwargs["aws_secret_access_key"] = aws_secret_access_key
            if aws_session_token:
                kwargs["aws_session_token"] = aws_session_token
        self._client = boto3.client("s3", **kwargs)

    def put(self, key: str, content: bytes, mime_type: str) -> str:
        """Upload bytes in-memory directly to S3. Returns the s3:// URL."""
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ContentType=mime_type,
                ServerSideEncryption=self._sse,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("vendor.s3.put_failed key=%s err=%r", key, e)
            raise
        return f"s3://{self._bucket}/{key}"

    def get_bytes(self, key_or_url: str) -> bytes:
        key = self._key(key_or_url)
        obj = self._client.get_object(Bucket=self._bucket, Key=key)
        return obj["Body"].read()

    def presigned_get(self, key_or_url: str, ttl_seconds: int = 300) -> str:
        key = self._key(key_or_url)
        try:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=ttl_seconds,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("vendor.s3.presign_failed key=%s err=%r", key, e)
            return ""

    def delete(self, key_or_url: str) -> None:
        """Best-effort delete. Used for cleanup on the failure paths
        (e.g. DB insert fails after S3 upload). Never raises — a leaked
        S3 object is far less bad than a 500 to the user.
        """
        try:
            key = self._key(key_or_url)
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except Exception as e:  # noqa: BLE001
            logger.warning("vendor.s3.delete_failed key=%s err=%r", key_or_url, e)

    def _key(self, key_or_url: str) -> str:
        if key_or_url.startswith("s3://"):
            rest = key_or_url[len("s3://"):]
            slash = rest.find("/")
            if slash == -1:
                raise ValueError(f"malformed s3 url: {key_or_url!r}")
            return rest[slash + 1:]
        return key_or_url


# ── factory ──────────────────────────────────────────────────────────────


_BACKEND_SINGLETON: _S3VendorStorage | None = None


def get_vendor_storage(settings=None) -> _S3VendorStorage:
    """Process-wide S3 backend. Reads VENDOR_S3_BUCKET (or RECEIPT_S3_BUCKET
    as a fallback) and AWS_DEFAULT_REGION.

    Resolution order:
        1. `settings.VENDOR_S3_BUCKET` / `settings.AWS_DEFAULT_REGION`
           (the lifespan-cached Settings — populated from `.env`)
        2. `os.environ` (the actual process environment — populated only
           when these vars were exported in the shell or by a process
           manager, NOT from `.env`)
        3. RECEIPT_S3_BUCKET as a convenience fallback for single-bucket
           deployments.

    Raises RuntimeError when none of the above yields a bucket name.
    The router catches RuntimeError and returns 503.
    """
    global _BACKEND_SINGLETON
    if _BACKEND_SINGLETON is not None:
        return _BACKEND_SINGLETON

    # Prefer the lifespan-cached Settings — pydantic-settings reads `.env`
    # into the Settings instance at startup; direct os.environ.get() does
    # not. Calling code should pass `request.app.state.settings`.
    bucket = ""
    region: str | None = None
    if settings is not None:
        bucket = (getattr(settings, "VENDOR_S3_BUCKET", "") or "").strip()
        region = getattr(settings, "AWS_DEFAULT_REGION", None) or None

    if not bucket:
        bucket = (
            os.environ.get("VENDOR_S3_BUCKET")
            or os.environ.get("RECEIPT_S3_BUCKET")
            or ""
        ).strip()
    if not bucket:
        raise RuntimeError(
            "VENDOR_S3_BUCKET env var is required. Vendor document uploads "
            "are S3-only — set VENDOR_S3_BUCKET (or RECEIPT_S3_BUCKET as a "
            "fallback) in your .env file (read by Settings at startup) or "
            "as a process env var."
        )
    if region is None:
        region = (
            os.environ.get("AWS_DEFAULT_REGION")
            or os.environ.get("AWS_REGION")
            or None
        )

    # Resolve AWS credentials: prefer Settings (which reads .env), then
    # process env. When all empty, leave them None → boto3 default chain
    # (env, ~/.aws/credentials, IAM role) runs at signing time.
    access_key = ""
    secret_key = ""
    session_token = ""
    if settings is not None:
        access_key = getattr(settings, "AWS_ACCESS_KEY_ID", "") or ""
        secret_key = getattr(settings, "AWS_SECRET_ACCESS_KEY", "") or ""
        session_token = getattr(settings, "AWS_SESSION_TOKEN", "") or ""
    if not access_key:
        access_key = os.environ.get("AWS_ACCESS_KEY_ID", "") or ""
    if not secret_key:
        secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "") or ""
    if not session_token:
        session_token = os.environ.get("AWS_SESSION_TOKEN", "") or ""

    _BACKEND_SINGLETON = _S3VendorStorage(
        bucket=bucket,
        region=region,
        aws_access_key_id=access_key or None,
        aws_secret_access_key=secret_key or None,
        aws_session_token=session_token or None,
    )
    return _BACKEND_SINGLETON
