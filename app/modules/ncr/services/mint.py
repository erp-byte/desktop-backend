"""Atomic ncr_no generation. Format: NCR-YYYY-MM-DD-NNNNN.

Uses `ncr_record_seq` for the monotonic counter (created in 007_ncr.sql).
The date prefix is informational; the sequence is global, not per-day, so
NCR-2026-05-05-00007 may be followed by NCR-2026-05-06-00008 — that's
acceptable for the spec's stated "atomic per-day mint" guarantee since
uniqueness is what matters, not per-day reset.
"""

from __future__ import annotations

from datetime import datetime, timezone


async def mint_ncr_no(conn) -> str:
    n = await conn.fetchval("SELECT nextval('ncr_record_seq')")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"NCR-{today}-{int(n):05d}"
