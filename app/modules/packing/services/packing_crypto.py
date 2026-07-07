"""AES-256-GCM symmetric encryption for packing "batch tokens".

The frontend holds an opaque, authenticated ciphertext (the batch token) that
encodes a plaintext ``batch_code``; the backend decrypts it with a server-only
256-bit key (``PACKING_TOKEN_KEY``) and processes it. The key never leaves the
backend.

AES-GCM is an *authenticated* cipher, so a tampered / truncated / wrong-key
token fails closed (``InvalidTag`` -> :class:`InvalidBatchToken`) rather than
returning garbage plaintext.

Token layout::

    urlsafe_b64( nonce[12 bytes] || ciphertext-with-tag )

Each encryption draws a fresh random 96-bit nonce, so the same ``batch_code``
produces a different token each time (non-deterministic — the frontend simply
stores whatever token it was issued).

Key resolution mirrors ``auth_service._get_cipher``: read the env var, fall
back to scanning the project ``.env`` files, and cache the cipher once per
process.
"""
from __future__ import annotations

import base64
import os
from functools import lru_cache
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# 96-bit nonce is the AES-GCM recommended/standard size.
_NONCE_BYTES = 12


class InvalidBatchToken(Exception):
    """A batch token could not be decoded or decrypted (malformed, tampered,
    or produced under a different key)."""


def _resolve_key() -> bytes:
    """Return the raw AES key bytes from ``PACKING_TOKEN_KEY``.

    The value is a urlsafe-base64 encoding of 16/24/32 random bytes. Read from
    ``os.environ`` first, then fall back to the project ``.env`` (root) and the
    CWD ``.env`` — same strategy as the auth cipher, so a ``.env``-only deploy
    works without exporting the key in the shell.
    """
    key = os.environ.get("PACKING_TOKEN_KEY")
    if not key:
        # packing_crypto.py -> services -> packing -> modules -> app -> ROOT
        root_env = Path(__file__).resolve().parents[4] / ".env"
        for env_path in (root_env, Path.cwd() / ".env"):
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith("PACKING_TOKEN_KEY="):
                        key = line.strip().split("=", 1)[1].strip()
                        break
            if key:
                break
    if not key:
        raise RuntimeError(
            "PACKING_TOKEN_KEY not set. Generate one with: "
            'python -c "import os,base64; '
            'print(base64.urlsafe_b64encode(os.urandom(32)).decode())"'
        )
    try:
        raw = base64.urlsafe_b64decode(key)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("PACKING_TOKEN_KEY is not valid urlsafe-base64") from exc
    if len(raw) not in (16, 24, 32):
        raise RuntimeError(
            f"PACKING_TOKEN_KEY must decode to 16/24/32 bytes (got {len(raw)}); "
            "regenerate a 32-byte key."
        )
    return raw


@lru_cache(maxsize=1)
def _cipher() -> AESGCM:
    return AESGCM(_resolve_key())


def encrypt_batch_code(batch_code: str) -> str:
    """Encrypt a batch_code into an opaque, URL-safe batch token."""
    nonce = os.urandom(_NONCE_BYTES)
    ct = _cipher().encrypt(nonce, batch_code.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt_batch_code(token: str) -> str:
    """Decrypt a batch token back to its plaintext batch_code.

    Raises :class:`InvalidBatchToken` on any malformed / tampered / wrong-key
    input so callers can map it to a 400.
    """
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        raise InvalidBatchToken("token is not valid base64") from exc
    if len(raw) <= _NONCE_BYTES:
        raise InvalidBatchToken("token too short to contain a nonce + ciphertext")
    nonce, ct = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    try:
        plaintext = _cipher().decrypt(nonce, ct, None)
    except InvalidTag as exc:
        raise InvalidBatchToken("token failed authentication") from exc
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidBatchToken("decrypted payload is not valid UTF-8") from exc
