"""Vendor documents uploaded in pieces.

Every API call from the web app reaches the backend through two proxies that
cap the request body: Netlify (the Next.js rewrite of /api/*, about 6 MB, less
for binary files) and the web server on EC2 in front of uvicorn (nginx's
default is 1 MB). A vendor's compliance documents run up to 25 MB each and 20
at a time, so posting them as one form failed with HTTP 413 before the request
reached this app.

So the browser sends a big file in pieces of CHUNK_BYTES, each well under both
caps, and the vendor endpoints then take the upload's id in place of the file:

    POST /api/v1/vendors/uploads                       {filename, content_type, size}
         -> {upload_id, chunk_bytes, chunks}
    PUT  /api/v1/vendors/uploads/{upload_id}/chunks/{index}   raw bytes of piece `index`
    POST /api/v1/vendors/extract-bulk   upload_ids=[...]  (or any endpoint taking a file)

The pieces wait on this machine's disk (VENDOR_UPLOAD_STAGING_DIR, else the
system temp folder). StagedUpload.read() joins them, checks the total against
the size declared at the start, and deletes them: an upload is used once.
Uploads older than STAGING_TTL are swept whenever a new one starts. Only the
user who started an upload can add to it or use it.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path

from fastapi import HTTPException

from app.modules.vendor.storage import MAX_VENDOR_UPLOAD_BYTES

# 768 KiB: under nginx's 1 MB default and, base64-encoded by Netlify (~1 MB),
# far under its limit too.
CHUNK_BYTES = 768 * 1024
STAGING_TTL = 6 * 3600
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _root() -> Path:
    base = os.environ.get("VENDOR_UPLOAD_STAGING_DIR") or tempfile.gettempdir()
    root = Path(base) / "candor_vendor_uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _dir(upload_id: str) -> Path:
    if not _ID_RE.match(upload_id or ""):
        raise HTTPException(400, detail={"code": "bad_upload_id"})
    return _root() / upload_id


def _meta(upload_id: str, user_id) -> tuple[Path, dict]:
    d = _dir(upload_id)
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, detail={"code": "upload_not_found",
                                         "msg": "The upload expired or was already used. Upload the file again."})
    if str(meta.get("user_id")) != str(user_id):
        raise HTTPException(404, detail={"code": "upload_not_found"})
    return d, meta


def _sweep() -> None:
    cutoff = time.time() - STAGING_TTL
    for d in _root().iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def start(user_id, filename: str, content_type: str | None, size: int) -> dict:
    if size <= 0:
        raise HTTPException(400, detail={"code": "empty_file"})
    if size > MAX_VENDOR_UPLOAD_BYTES:
        raise HTTPException(413, detail={"code": "file_too_large", "limit_bytes": MAX_VENDOR_UPLOAD_BYTES,
                                         "actual_bytes": size})
    _sweep()
    upload_id = secrets.token_hex(16)
    d = _root() / upload_id
    d.mkdir()
    chunks = math.ceil(size / CHUNK_BYTES)
    meta = {"user_id": str(user_id), "filename": (filename or "")[:255], "content_type": content_type,
            "size": size, "chunks": chunks}
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return {"upload_id": upload_id, "chunk_bytes": CHUNK_BYTES, "chunks": chunks}


def put_chunk(user_id, upload_id: str, index: int, data: bytes) -> dict:
    d, meta = _meta(upload_id, user_id)
    if not 0 <= index < meta["chunks"]:
        raise HTTPException(400, detail={"code": "bad_chunk_index", "chunks": meta["chunks"]})
    last = index == meta["chunks"] - 1
    expected = meta["size"] - CHUNK_BYTES * index if last else CHUNK_BYTES
    if len(data) != expected:
        raise HTTPException(400, detail={"code": "bad_chunk_size", "expected": expected, "actual": len(data)})
    tmp = d / f"{index:05d}.part.tmp"
    tmp.write_bytes(data)
    tmp.replace(d / f"{index:05d}.part")       # a retried piece simply replaces itself
    return {"upload_id": upload_id, "index": index}


class StagedUpload:
    """Stands in for a FastAPI UploadFile: filename, content_type, async read()."""

    def __init__(self, upload_id: str, user_id):
        self._dir, meta = _meta(upload_id, user_id)
        self.filename: str = meta["filename"]
        self.content_type: str | None = meta["content_type"]
        self._size: int = meta["size"]
        self._chunks: int = meta["chunks"]
        missing = [i for i in range(self._chunks) if not (self._dir / f"{i:05d}.part").exists()]
        if missing:
            raise HTTPException(400, detail={"code": "upload_incomplete", "missing_chunks": missing[:20]})

    async def read(self) -> bytes:
        data = b"".join((self._dir / f"{i:05d}.part").read_bytes() for i in range(self._chunks))
        shutil.rmtree(self._dir, ignore_errors=True)   # used once
        if len(data) != self._size:
            raise HTTPException(400, detail={"code": "upload_corrupt", "expected": self._size, "actual": len(data)})
        return data


def staged_files(upload_ids: str | list[str] | None, user_id) -> list[StagedUpload]:
    """The uploads named by a JSON list (or list) of ids, in order."""
    if not upload_ids:
        return []
    try:
        ids = json.loads(upload_ids) if isinstance(upload_ids, str) else upload_ids
    except ValueError:
        ids = None
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPException(400, detail={"code": "bad_upload_ids"})
    return [StagedUpload(i, user_id) for i in ids]
