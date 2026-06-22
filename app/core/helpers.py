"""Shared helper functions used across modules."""

import asyncio
import re
import time
import unicodedata
from datetime import datetime
from typing import Awaitable, Callable, TypeVar
from zoneinfo import ZoneInfo

from asyncpg.exceptions import UniqueViolationError


# IST = India Standard Time (UTC+05:30, no DST).  Used by the JC batch
# open/close flow which stores both a canonical UTC TIMESTAMPTZ and a
# human-readable IST literal so reports / psql queries surface the
# clock face the floor operator saw — no timezone setup required.
_IST_TZ = ZoneInfo("Asia/Kolkata")


def ist_now_text() -> str:
    """Current wall-clock time in IST, formatted as 'YYYY-MM-DD HH:MM:SS IST'.

    Use for the *_at_ist text columns added by migration 038.  The
    canonical TIMESTAMPTZ columns (started_at, closed_at, ended_at)
    continue to use NOW() — those stay in UTC under the hood and remain
    the source of truth for ordering / math.  The IST literal is a
    sibling for human reading only.
    """
    return datetime.now(_IST_TZ).strftime("%Y-%m-%d %H:%M:%S IST")


# Defaults for the time-ID savepoint-retry pattern. Inserts colliding on a
# time-based PK retry with a fresh ID after a small sleep so the epoch-ms
# counter advances. Tunables exported so callers can override per site.
MAX_PK_RETRIES = 3
PK_RETRY_DELAY_S = 0.002


def new_short_time_id() -> int:
    """8-digit time-based short ID (last 8 digits of epoch-ms).

    Used as the application-supplied PK for tables where we want a stable,
    short, non-sequential identifier (e.g. so_line.so_line_id,
    so_fulfillment_v2.so_fulfillment_id). Equivalent to:
        int(str(int(time.time() * 1000))[-8:])

    Collisions are possible if two inserts land in the same millisecond —
    callers should use insert_with_pk_retry() to handle them gracefully.
    """
    return int(str(int(time.time() * 1000))[-8:])


T = TypeVar("T")


async def insert_with_pk_retry(
    conn,
    insert_callable: Callable[[], Awaitable[T]],
    *,
    max_retries: int = MAX_PK_RETRIES,
    delay_s: float = PK_RETRY_DELAY_S,
) -> T:
    """Run an async INSERT inside a SAVEPOINT, retrying on PK collisions.

    `insert_callable` is a no-arg async callable that performs the INSERT and
    returns whatever asyncpg returns (e.g. the row from fetchrow, or the
    status string from execute). It MUST generate a fresh time-based ID on
    each call so retries can succeed:

        async def _insert():
            return await conn.fetchrow(
                "INSERT INTO foo (id, ...) VALUES ($1, ...) RETURNING id",
                new_short_time_id(), ...
            )
        row = await insert_with_pk_retry(conn, _insert)

    Behaviour:
      - On UniqueViolationError where constraint name contains '_pkey':
        sleep `delay_s` and retry, up to `max_retries` total attempts.
      - On any other UniqueViolationError (e.g. (so_line_id, fy) collision
        not handled by ON CONFLICT): re-raise immediately — non-PK conflicts
        are NOT silently swallowed.
      - After exhausting retries on a PK collision: re-raise the final
        exception so the caller decides what to do.

    Must be called inside an outer transaction; `conn.transaction()` here
    creates a SAVEPOINT so rollback affects only this row.
    """
    # Defensive precondition: without an outer txn, conn.transaction() starts
    # a real transaction and a PK-retry would commit partial state.
    if not conn.is_in_transaction():
        raise RuntimeError(
            "insert_with_pk_retry must be called inside an outer transaction "
            "(use `async with conn.transaction(): ...` at the caller level)"
        )
    for attempt in range(max_retries):
        try:
            async with conn.transaction():  # savepoint
                return await insert_callable()
        except UniqueViolationError as exc:
            constraint = getattr(exc, "constraint_name", "") or ""
            if "pkey" not in constraint:
                raise  # B3: non-PK violations propagate
            if attempt < max_retries - 1:
                await asyncio.sleep(delay_s)
                continue
            raise  # PK collision after all retries


# ---------------------------------------------------------------------------
# Key normalisation for master-data joins (SFG / Job-Card ingest)
# ---------------------------------------------------------------------------

# Whitespace code points that leak into spreadsheet exports and break an
# exact-string join against all_sku: NBSP, narrow-NBSP, thin/hair spaces,
# the various Unicode spaces, and zero-width characters (which are deleted,
# not turned into a space).
_NBSP_LIKE = (
    " "  # NO-BREAK SPACE
    " "  # FIGURE SPACE
    " "  # THIN SPACE
    " "  # HAIR SPACE
    " "  # NARROW NO-BREAK SPACE
    "　"  # IDEOGRAPHIC SPACE
)
_ZERO_WIDTH = "​‌‍﻿"  # ZWSP, ZWNJ, ZWJ, BOM
_ZERO_WIDTH_RE = re.compile(f"[{_ZERO_WIDTH}]")
_WS_RUN_RE = re.compile(r"\s+")

# Lead byte of "UTF-8 punctuation decoded as cp1252" mojibake — Â (0xC2-block:
# NBSP, ©, °, …), Ã (0xC3-block: accented latin), â (0xE2-block: the smart
# quotes / dash / ellipsis / bullet whose mojibake is 'â€X'). Triggering on the
# lead alone (not a fragile 2-char window) catches the common smart-quote /
# ellipsis cases the old window missed.
_MOJIBAKE_LEAD_RE = re.compile(r"[ÂÃâ]")
# C1 control chars (U+0080..U+009F) are the signature of a WRONG cp1252->utf-8
# guess; if the round-trip produces any, reject the repair so legitimate strings
# that merely contain Â/Ã/â are never mangled.
_C1_CONTROL_RE = re.compile(r"[\x80-\x9f]")


def normalise_key(s) -> str:
    """Canonicalise a master-data string before an exact join to all_sku.

    Repairs the spreadsheet-export gotchas that silently break exact-string
    matches: NBSP / narrow-NBSP / thin spaces collapse to a single regular
    space, zero-width characters are removed, common UTF-8→cp1252 mojibake is
    repaired, the result is NFC-normalised and whitespace-collapsed/stripped.

    Returns "" for None/empty. Idempotent for clean and single-layer-mojibake
    inputs: normalise_key(normalise_key(x)) == normalise_key(x). (Doubly-encoded
    mojibake needs more than one pass and is out of scope.)

        >>> normalise_key('SFG0123\\xa0')
        'SFG0123'
        >>> normalise_key('Cafe  Galleries\\xa0Sunflower  Seeds')
        'Cafe Galleries Sunflower Seeds'
    """
    if s is None:
        return ""
    s = str(s)
    # Conservative mojibake repair — only when a lead marker is present, and
    # only keep the result if it cleanly round-trips (strict) AND introduces no
    # C1 control chars (a wrong guess's signature).
    if _MOJIBAKE_LEAD_RE.search(s):
        try:
            repaired = s.encode("cp1252", "strict").decode("utf-8", "strict")
            if not _C1_CONTROL_RE.search(repaired):
                s = repaired
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass  # not actually cp1252-mojibake — leave as-is
    # Drop zero-width chars, fold NBSP-likes to a plain space. (The translate is
    # belt-and-suspenders: Python re \s is Unicode-aware so the \s+ collapse
    # below would catch these too — kept explicit for documentation.)
    s = _ZERO_WIDTH_RE.sub("", s)
    s = s.translate({ord(c): " " for c in _NBSP_LIKE})
    # Canonical composition so visually-identical strings compare equal.
    s = unicodedata.normalize("NFC", s)
    # Collapse internal whitespace runs and trim.
    return _WS_RUN_RE.sub(" ", s).strip()


# Advisory-lock key that serialises concurrent SFG#### allocations (arbitrary
# constant; only next_sfg_code uses it).
SFG_CODE_LOCK = 0x5F67  # ~ 'sfg' (shared by next_sfg_code + ingest_sfg_master)
# Serialises concurrent process-category classification backfills (Slice 2).
# Distinct from SFG_CODE_LOCK and outside the 8-digit job-card-id lock space.
PROCESS_CLASS_LOCK = 0x5F68
# Serialises concurrent JC-routing loads (Slice 3) — replacing an article's
# bom_process_route with its authoritative 2-stage chain. Distinct from the two
# above; also outside the 8-digit job-card-id lock space.
ROUTING_LOCK = 0x5F69
# Serialises concurrent bar-line process overrides (G4) — re-deriving a
# bom_process_route from bom_header.bar_line_process. Distinct from the three
# above; also outside the 8-digit job-card-id lock space.
BAR_LINE_LOCK = 0x5F6A
# Serialises concurrent SFG-seam minting (Phase 9) — minting/reusing SFG####
# codes for Create-WIP transform steps that lack a seam and stamping
# output_code/input_code across the chain. Distinct from the four above; also
# outside the 8-digit job-card-id lock space. NOTE: the seam minter takes BOTH
# this lock AND SFG_CODE_LOCK (via next_sfg_code) — always in that order — so
# the new-SFG#### allocation it performs cannot race ingest_sfg_master.
SEAM_LOCK = 0x5F6B


async def next_sfg_code(conn, *, width: int = 4) -> str:
    """Allocate the next ``SFG####`` code, strictly above the highest in all_sku.

    Reads ``MAX(numeric-part)`` of existing ``item_type='sfg'`` rows and returns
    the successor, zero-padded to ``width`` (overflowing past the width is fine —
    ``SFG10000`` once 9999 is exceeded). Empty catalogue → ``SFG0001``.

    Concurrency-safe: takes a transaction-scoped advisory lock so two callers
    can never observe the same MAX and mint a duplicate. MUST run inside an
    outer transaction (the lock auto-releases at COMMIT/ROLLBACK); raises if not.

    PRECONDITION: the caller MUST persist the returned code into all_sku (the
    table this reads MAX() from) within the SAME transaction before COMMIT —
    the lock serialises readers but does not itself reserve the code, so
    allocating into another table alone would re-introduce collisions.
    """
    if not conn.is_in_transaction():
        raise RuntimeError(
            "next_sfg_code must be called inside an outer transaction "
            "(use `async with conn.transaction(): ...` at the caller level)"
        )
    await conn.execute("SELECT pg_advisory_xact_lock($1)", SFG_CODE_LOCK)
    max_num = await conn.fetchval(
        """
        SELECT COALESCE(MAX(substring(sfg_code FROM '[0-9]+')::int), 0)
          FROM all_sku
         WHERE item_type = 'sfg' AND sfg_code ~ '^SFG[0-9]+$'
        """
    )
    return f"SFG{(max_num or 0) + 1:0{width}d}"


def safe_float(val) -> float | None:
    """Parse value to float rounded to 3dp, return None on failure."""
    if val is None:
        return None
    try:
        return round(float(val), 3)
    except (ValueError, TypeError):
        return None


def safe_float_zero(val) -> float:
    """Parse value to float rounded to 3dp, return 0.0 on failure."""
    if val is None:
        return 0.0
    try:
        return round(float(val), 3)
    except (ValueError, TypeError):
        return 0.0


def safe_str(val) -> str | None:
    """Strip string, return None if empty."""
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None
