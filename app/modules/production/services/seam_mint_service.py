"""Phase 9 — SFG seam minting for transform chains (opt-in, idempotent, dedup-correct).

Bar-line-routed (061) and gap-promoted (058) FGs grow ``bom_process_route`` rows
that "Create WIP" (stage_bucket='Create WIP') but carry NO SFG#### seam — their
``output_code`` is NULL. A Create-WIP step with no ``output_code`` has no chain
seam: there is nothing for the downstream stage's ``input_code`` to consume (the
JC1.output_code -> JC2.input_code handoff that 050 set up). Such chains can
therefore never spawn a real SFG / WIP batch.

This module mints (or REUSES) an SFG#### for each such step and stamps the seam:
the Create-WIP step gets ``output_code``/``output_kind='SFG'`` and the NEXT step
in the same bom (next step_number) gets ``input_code``/``input_kind='SFG'``
pointing at the same code — mirroring ``ingest_jc_routing``. Multi-transform
chains are handled: step k produces SFG_k, consumed by step k+1; if step k+1 is
ALSO a Create-WIP transform it mints its OWN SFG_{k+1} for the step after it.

────────────────────────────────────────────────────────────────────────────────
DEDUP CONTRACT  (the part that must be exactly right)
────────────────────────────────────────────────────────────────────────────────
Naive "mint one SFG per Create-WIP step" creates DUPLICATE SFGs for FGs that are
the same recipe in different pack sizes (e.g. "Almond Bar 30g" and "Almond Bar
50g" both Blend & Form the same intermediate). To avoid that, every step is
reduced to a DEDUP KEY:

    key = ( normalise_key(_base_recipe(fg_sku_name)),
            normalise_key(create_wip_operation) )

  * ``_base_recipe(name)`` strips pack-size / marketing tokens from the FG name
    so two pack variants collapse to the same recipe family (see _base_recipe).
  * ``create_wip_operation`` is the step's ``practical_operation`` (fallback
    ``process_name``) — e.g. "Blend & Form", "Roasting", "Roast & Flavour/Salt".

LOOKUP-BEFORE-MINT: before minting, an in-memory index of
``(normalise_key(base_recipe), normalise_key(create_wip_operation)) -> sfg_code``
is built ONCE from the EXISTING catalogue (sfg_attributes joined to all_sku). If
a step's key is already in that index the existing sfg_code is REUSED — no new
SFG is created. Only when no existing SFG matches is a fresh one minted. Newly
minted codes are ADDED to the in-memory index as we go, so two same-recipe steps
within ONE run share ONE minted SFG (minted=1, not 2).

KNOWN LIMITATION — NO recipe-signature split (the ``_vN`` cases): the design ref
splits same-base+operation SFGs further by a BOM ingredient signature when the
recipes genuinely differ. These FGs (bar-line / gap-promoted) carry NO bom_line
ingredient data, so there is no signature to split on — two FGs with the same
base_recipe + operation therefore SHARE ONE SFG here even if their real recipes
differ. This is a deliberate, documented limitation, not a bug. Minted rows are
tagged ``sfg_origin='SEAM_MINTED'`` so they can be re-split later if ingredient
data ever lands.

────────────────────────────────────────────────────────────────────────────────
WHY OPT-IN (script-only, NOT wired into run_master_ingest startup)
────────────────────────────────────────────────────────────────────────────────
Minting catalogue rows + mutating routing seams is a deliberate, audited action.
Run it AFTER apply_bar_line_override / promote_fg_master_gaps via
``scripts/mint_sfg_seam.py``. On CURRENT prod there are 0 such steps (pack-only
promotions need no seam), so this is exercised entirely on synthetic data.

Guards on the all_sku.sfg_code column (051): if absent, skips with a warning.
Writes ``sfg_origin='SEAM_MINTED'`` to sfg_attributes always; also to
all_sku.sfg_origin ONLY if that column exists (so a DB without it is unaffected).
"""

from __future__ import annotations

import logging
import re

from app.core.helpers import (
    SEAM_LOCK,
    insert_with_pk_retry,
    new_short_time_id,
    next_sfg_code,
    normalise_key,
)

logger = logging.getLogger(__name__)

# stage_bucket value marking a step that produces a WIP/SFG (see master_ingest
# STAGE_CREATE_WIP). Kept local so this module does not couple to the classifier.
STAGE_CREATE_WIP = "Create WIP"
SEAM_ORIGIN = "SEAM_MINTED"


# ───────────────────────────────────────────────────────────────────────────
# _base_recipe — strip pack-size / marketing tokens from an FG name so two pack
# variants of the same recipe collapse to ONE dedup family. (design-ref §3 step 4)
# ───────────────────────────────────────────────────────────────────────────
#
# CONSERVATIVE BY DESIGN: only well-known, low-ambiguity pack/size/id tokens are
# stripped. Anything ambiguous is LEFT IN — over-stripping wrongly merges two
# different recipes into one SFG (the worse failure), so we err toward keeping
# tokens. Each rule below is documented; add new tokens only with a test.

# (1234567) trailing numeric id suffix, e.g. "... (1023456)".
_ID_SUFFIX_RE = re.compile(r"\(\s*\d{4,}\s*\)\s*$")
# ASIN-like code: B + 9 alphanumerics (Amazon standard id), as a whole token.
_ASIN_RE = re.compile(r"\bB0[A-Z0-9]{8}\b", re.IGNORECASE)
# "pack of N" / "pack of 12" marketing token.
_PACK_OF_RE = re.compile(r"\bpack\s+of\s+\d+\b", re.IGNORECASE)
# "1*1", "12 x 50", "2 X 100", "12 x 50g", "6x100g" multiplier groups
# (count x size, with an optional unit suffix on the size). Anchored on the first
# number's \b so it never starts mid-word; the optional unit eats "...g"/"...gm"
# so no bare unit letter is left dangling.
_MULTIPLIER_RE = re.compile(
    r"\b\d+\s*[x*]\s*\d+(?:\.\d+)?\s*"
    r"(?:gms?|gm|grams?|g|kgs?|kg|mls?|ml|ltrs?|ltr|litres?|liters?|l|ozs?|oz|lbs?|lb)?",
    re.IGNORECASE,
)
# A weight/size token: <number><unit> with optional space, unit ∈ g/gm/gms/kg/
# kgs/ml/l/ltr/litre(s)/oz/lb. Word-boundaryed so "g" inside a word is safe.
_SIZE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*"
    r"(?:gms?|gm|grams?|g|kgs?|kg|mls?|ml|ltrs?|ltr|litres?|liters?|l|ozs?|oz|lbs?|lb)\b",
    re.IGNORECASE,
)
# A trailing run of pure-number / separator tokens (e.g. "... 100 250"): only at
# the END, so a leading number that is part of the recipe name is preserved.
_TRAILING_NUMS_RE = re.compile(r"(?:[\s,*×/+\-]+\d+(?:\.\d+)?)+\s*$")
# Collapse whitespace + dangling separators left behind by the removals. NOTE:
# 'x' is deliberately NOT in this char class — it would wrongly eat the trailing
# letter of words like "mix"/"max". 'x'-as-multiplier is already handled by
# _MULTIPLIER_RE / _TRAILING_NUMS_RE (which require an adjacent digit).
_WS_RE = re.compile(r"\s+")
_DANGLING_SEP_RE = re.compile(r"[\s,\-–—*×/+]+$|^[\s,\-–—*×/+]+")


def _base_recipe(name: str | None) -> str:
    """Reduce an FG name to its pack-size-stripped recipe family for dedup.

    Lowercases, then removes (in order): trailing "(1234567)" id suffix, ASIN-like
    codes, "pack of N", "N x M" multipliers, weight/size tokens ("100gm","1 kg",
    "48 gms","250G","500 Gm","1L"…), then a trailing run of bare numbers, then
    collapses whitespace and trims dangling separators. Empty / None -> "".

    Conservative on purpose (see module docstring): only well-known tokens are
    stripped, so two genuinely-different recipes are never merged by over-eager
    stripping. The result is NOT normalise_key'd here — callers normalise_key the
    base_recipe when forming the dedup key, so this stays a pure shaping step.

        >>> _base_recipe("Almond Bar 30g")            -> "almond bar"
        >>> _base_recipe("Roasted Cashew 100 gm")     -> "roasted cashew"
        >>> _base_recipe("Trail Mix 1*1 (1023456)")   -> "trail mix"
        >>> _base_recipe("Date Bar Pack of 12 250G")  -> "date bar"
    """
    if not name:
        return ""
    s = str(name).strip().lower()
    s = _ID_SUFFIX_RE.sub(" ", s)
    s = _ASIN_RE.sub(" ", s)
    s = _PACK_OF_RE.sub(" ", s)
    s = _MULTIPLIER_RE.sub(" ", s)
    # Size tokens can repeat / chain ("12 x 50g 250 gm"); loop until stable.
    prev = None
    while prev != s:
        prev = s
        s = _SIZE_RE.sub(" ", s)
    s = _TRAILING_NUMS_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    s = _DANGLING_SEP_RE.sub("", s).strip()
    s = _WS_RE.sub(" ", s).strip()
    return s


def _dedup_key(fg_sku_name: str | None, operation: str | None) -> tuple[str, str]:
    """The canonical dedup key: (normalise_key(base_recipe), normalise_key(op))."""
    return (
        normalise_key(_base_recipe(fg_sku_name)),
        normalise_key(operation),
    )


def _titled(base_recipe: str) -> str:
    """Title-case the (lowercase) base_recipe for the SFG display particulars."""
    return base_recipe.title() if base_recipe else "WIP"


# ───────────────────────────────────────────────────────────────────────────
# Schema guards
# ───────────────────────────────────────────────────────────────────────────

async def _all_sku_has_sfg_code(conn) -> bool:
    """True if all_sku.sfg_code (added by 051) exists — the hard prerequisite."""
    return bool(await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_name = 'all_sku' AND column_name = 'sfg_code'
        )
        """
    ))


async def _column_exists(conn, table: str, column: str) -> bool:
    return bool(await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_name = $1 AND column_name = $2
        )
        """,
        table, column,
    ))


async def _build_existing_index(conn) -> dict[tuple[str, str], str]:
    """In-memory dedup index of EXISTING catalogue SFGs:
    (normalise_key(base_recipe), normalise_key(create_wip_operation)) -> sfg_code.

    Built ONCE from sfg_attributes joined to all_sku (only rows that carry BOTH a
    base_recipe and a create_wip_operation can form a key). normalise_key is
    Python-only so the key is computed here, not in SQL. On a base_recipe+op
    collision the lexicographically smallest sfg_code wins (deterministic reuse).
    """
    index: dict[tuple[str, str], str] = {}
    rows = await conn.fetch(
        """
        SELECT a.sfg_code, a.base_recipe, a.create_wip_operation
          FROM sfg_attributes a
          JOIN all_sku s ON s.sfg_code = a.sfg_code AND s.item_type = 'sfg'
         WHERE a.base_recipe IS NOT NULL
           AND a.create_wip_operation IS NOT NULL
        """
    )
    for r in rows:
        key = (normalise_key(r["base_recipe"]), normalise_key(r["create_wip_operation"]))
        if not key[0] or not key[1]:
            continue
        code = r["sfg_code"]
        if key not in index or code < index[key]:
            index[key] = code
    return index


async def _sfg_attributes_exists(conn) -> bool:
    return bool(await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'sfg_attributes')"
    ))


# ───────────────────────────────────────────────────────────────────────────
# Mint
# ───────────────────────────────────────────────────────────────────────────

async def _mint_sfg(conn, base_recipe: str, operation: str, *,
                    write_all_sku_origin: bool, write_attrs: bool) -> str:
    """Allocate + persist ONE net-new SFG#### for (base_recipe, operation).

    Inserts an all_sku row (item_type='sfg', sfg_code, sale_group='wip', 8-digit
    BIGINT sku_id via new_short_time_id + insert_with_pk_retry) and (if the table
    exists) the matching sfg_attributes row (base_recipe / create_wip_operation /
    sfg_origin='SEAM_MINTED'). Returns the new sfg_code. MUST run inside an outer
    transaction (next_sfg_code + insert_with_pk_retry both require it).
    """
    code = await next_sfg_code(conn)
    particulars = f"{_titled(base_recipe)} · WIP · {operation}"

    if write_all_sku_origin:
        async def _insert(_code=code, _name=particulars):
            return await conn.fetchval(
                """
                INSERT INTO all_sku
                    (sku_id, sfg_code, particulars, item_type, sale_group, sfg_origin)
                VALUES ($1, $2, $3, 'sfg', 'wip', $4)
                ON CONFLICT (sfg_code) WHERE sfg_code IS NOT NULL DO NOTHING
                RETURNING sku_id
                """,
                new_short_time_id(), _code, _name, SEAM_ORIGIN,
            )
    else:
        async def _insert(_code=code, _name=particulars):
            return await conn.fetchval(
                """
                INSERT INTO all_sku
                    (sku_id, sfg_code, particulars, item_type, sale_group)
                VALUES ($1, $2, $3, 'sfg', 'wip')
                ON CONFLICT (sfg_code) WHERE sfg_code IS NOT NULL DO NOTHING
                RETURNING sku_id
                """,
                new_short_time_id(), _code, _name,
            )

    await insert_with_pk_retry(conn, _insert, max_retries=5)

    if write_attrs:
        await conn.execute(
            """
            INSERT INTO sfg_attributes
                (sfg_code, base_recipe, create_wip_operation, sfg_origin)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (sfg_code) DO UPDATE SET
                base_recipe          = EXCLUDED.base_recipe,
                create_wip_operation = EXCLUDED.create_wip_operation,
                sfg_origin           = EXCLUDED.sfg_origin
            """,
            code, base_recipe, operation, SEAM_ORIGIN,
        )
    return code


# ───────────────────────────────────────────────────────────────────────────
# Public entry point
# ───────────────────────────────────────────────────────────────────────────

async def mint_sfg_seam_for_chains(conn, *, dry_run: bool = True,
                                   only_bom_id: int | None = None) -> dict:
    """Mint/reuse SFG#### codes for unseamed Create-WIP steps + stamp the seam.

    Scans ``bom_process_route`` for Create-WIP steps (stage_bucket='Create WIP')
    whose ``output_code`` is NULL/blank (covers ALL such steps — bar-line-routed,
    gap-promoted, or otherwise), groups them by bom_id, and per chain (ordered by
    step_number):

      * computes the dedup key (normalise_key(_base_recipe(fg_sku_name)),
        normalise_key(operation)) where operation = practical_operation (fallback
        process_name);
      * REUSES an existing sfg_code if the key is already in the catalogue index
        (built once) or was minted earlier in THIS run; else MINTS a fresh one
        (and adds it to the index so same-key later steps reuse it -> minted=1,
        not 2, for two same-recipe FGs);
      * stamps the producer step: output_code=<sfg>, output_kind='SFG';
      * stamps the CONSUMER step (the next step_number in the same bom):
        input_code=<sfg>, input_kind='SFG'.

    Idempotent: a step that already has a non-blank output_code is SKIPPED (it
    still feeds the dedup index, so re-mints converge). A second full run mints 0.

    MUST run inside an outer transaction (next_sfg_code / insert_with_pk_retry use
    savepoints). On write it takes a transaction-scoped advisory lock (SEAM_LOCK)
    so concurrent minters cannot allocate duplicate codes.

    dry_run=True (default): no writes — reports what WOULD be minted / reused /
    seamed (dedup is fully simulated against the in-memory index so the counts are
    exact). Guards on all_sku.sfg_code (051): if absent, status='schema_missing'.

    Returns {status, dry_run, minted, reused, steps_seamed, chains, sample}.
    """
    if not await _all_sku_has_sfg_code(conn):
        logger.warning(
            "SFG seam minting skipped — all_sku.sfg_code missing; "
            "run migration 051 first"
        )
        return {"status": "schema_missing", "dry_run": dry_run,
                "minted": 0, "reused": 0, "steps_seamed": 0,
                "chains": 0, "sample": []}

    if not dry_run:
        # Serialise concurrent minters: the read-MAX-then-mint in next_sfg_code +
        # the seam stamping must not interleave across transactions.
        await conn.execute("SELECT pg_advisory_xact_lock($1)", SEAM_LOCK)

    write_attrs = await _sfg_attributes_exists(conn)
    write_all_sku_origin = await _column_exists(conn, "all_sku", "sfg_origin")

    # Dedup index seeded from the existing catalogue (only if sfg_attributes is
    # present — without it there are no existing keys to reuse, only fresh mints).
    index: dict[tuple[str, str], str] = (
        await _build_existing_index(conn) if write_attrs else {}
    )

    # Pull every Create-WIP step lacking a seam, plus EVERY step of those boms
    # (we need the next step_number to stamp the consumer). Restrict to boms that
    # actually have a pending Create-WIP step. $1=STAGE_CREATE_WIP always; $2 is
    # the optional only_bom_id filter (NULL -> no restriction).
    rows = await conn.fetch(
        """
        SELECT r.bom_id, r.step_number, r.process_name, r.practical_operation,
               r.stage_bucket, r.output_code, r.input_code,
               h.fg_sku_name
          FROM bom_process_route r
          JOIN bom_header h ON h.bom_id = r.bom_id
         WHERE r.bom_id IN (
                   SELECT bom_id FROM bom_process_route
                    WHERE stage_bucket = $1
                      AND (output_code IS NULL OR btrim(output_code) = '')
               )
           AND ($2::int IS NULL OR r.bom_id = $2)
         ORDER BY r.bom_id, r.step_number
        """,
        STAGE_CREATE_WIP, only_bom_id,
    )

    # Group rows into ordered chains by bom_id.
    chains: dict[int, list] = {}
    for r in rows:
        chains.setdefault(r["bom_id"], []).append(r)

    # Pre-pass: seed the dedup index from steps that ALREADY carry a seam, before
    # processing any unseamed step. Without this the reuse outcome is order-
    # dependent — an unseamed step in an earlier bom would mint/reuse a catalogue
    # code while a same-recipe already-seamed step in a later bom is only seen
    # afterwards, so two "same recipe" chains could land on different codes. An
    # in-use seam is authoritative over a catalogue-only entry; on collision keep
    # the lexicographically smallest (same rule as _build_existing_index).
    for steps in chains.values():
        fg_name = steps[0]["fg_sku_name"]
        for step in steps:
            if (step["stage_bucket"] or "") != STAGE_CREATE_WIP:
                continue
            existing_out = step["output_code"]
            if existing_out is None or str(existing_out).strip() == "":
                continue
            operation = (step["practical_operation"]
                         or step["process_name"] or "").strip()
            key = _dedup_key(fg_name, operation)
            if not key[0] or not key[1]:
                continue
            code = str(existing_out).strip()
            if key not in index or code < index[key]:
                index[key] = code

    minted = 0
    reused = 0
    steps_seamed = 0
    sample: list[dict] = []

    for bom_id, steps in chains.items():
        fg_name = steps[0]["fg_sku_name"]
        for idx, step in enumerate(steps):
            bucket = (step["stage_bucket"] or "")
            existing_out = step["output_code"]
            is_create_wip = bucket == STAGE_CREATE_WIP
            already_seamed = existing_out is not None and str(existing_out).strip() != ""

            if not is_create_wip:
                continue

            operation = (step["practical_operation"]
                         or step["process_name"] or "").strip()
            key = _dedup_key(fg_name, operation)

            if already_seamed:
                # Idempotent: keep the existing seam, but FEED the dedup index so a
                # later same-key step reuses this code instead of minting a dup.
                if key[0] and key[1] and key not in index:
                    index[key] = str(existing_out).strip()
                continue

            # Reuse-before-mint.
            if key[0] and key[1] and key in index:
                sfg_code = index[key]
                reused += 1
                origin = "reused"
            else:
                base_recipe = _base_recipe(fg_name)
                if dry_run:
                    # Simulate a fresh code so same-key later steps in the dry-run
                    # also "reuse" it and the minted count is exact. The sentinel
                    # is never written.
                    sfg_code = f"SFG?MINT{minted + 1}"
                else:
                    sfg_code = await _mint_sfg(
                        conn, base_recipe, operation,
                        write_all_sku_origin=write_all_sku_origin,
                        write_attrs=write_attrs,
                    )
                minted += 1
                origin = "minted"
                if key[0] and key[1]:
                    index[key] = sfg_code

            # Stamp the producer step (output) + the consumer step (next in bom).
            consumer = steps[idx + 1] if idx + 1 < len(steps) else None
            if not dry_run:
                await conn.execute(
                    "UPDATE bom_process_route "
                    "SET output_code = $1, output_kind = 'SFG' "
                    "WHERE bom_id = $2 AND step_number = $3",
                    sfg_code, bom_id, step["step_number"],
                )
                if consumer is not None:
                    await conn.execute(
                        "UPDATE bom_process_route "
                        "SET input_code = $1, input_kind = 'SFG' "
                        "WHERE bom_id = $2 AND step_number = $3",
                        sfg_code, bom_id, consumer["step_number"],
                    )
            steps_seamed += 1

            if len(sample) < 20:
                sample.append({
                    "bom_id": bom_id,
                    "fg_sku_name": fg_name,
                    "step_number": step["step_number"],
                    "operation": operation,
                    "base_recipe": _base_recipe(fg_name),
                    "sfg_code": sfg_code,
                    "origin": origin,
                    "consumer_step": consumer["step_number"] if consumer else None,
                })

    logger.info(
        "SFG seam minting (%s): %d chains, %d minted, %d reused, %d steps seamed",
        "DRY RUN" if dry_run else "APPLIED",
        len(chains), minted, reused, steps_seamed,
    )
    return {
        "status": "ok",
        "dry_run": dry_run,
        "minted": minted,
        "reused": reused,
        "steps_seamed": steps_seamed,
        "chains": len(chains),
        "sample": sample,
    }
