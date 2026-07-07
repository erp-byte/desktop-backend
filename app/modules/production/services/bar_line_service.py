"""G4 — Bar-Line Process override.

84 bar-line value-added FGs carry a RICHER routing in
``bom_header.bar_line_process`` than the one derived from their
``process_category`` column, but that richer string is unused. This module
turns it into an OPT-IN, idempotent, audited capability: re-derive those FGs'
``bom_process_route`` from ``bar_line_process``.

The two distinct bar_line_process strings (84 FGs) are::

    (82) Receiving + Sorting + Weighing + Roasting + Mixing + Krugger + Flow
         + X-ray + Monocarton + Master Carton
    (2)  De-Seeding+Sorting+Stuffing+Enrobing+Flow Wrap+Sorting+Master Carton

Both are fully classifiable by master_ingest.classify_route_steps once the
G4 token additions ('mixing' -> Blend & Form transform; 'flow' -> terminal)
are in the maps — so applying them never leaves an UNCLASSIFIED (NULL
stage_bucket) step that the warm-path classifier would re-select forever (the
same bug that was just fixed for "packing").

WHY OPT-IN (script-only, NOT wired into run_master_ingest startup): this
OVERRIDES routing that already works (the process_category-derived route). It
must be a deliberate, audited action, never an automatic startup mutation.

FOLLOW-UP (intentionally NOT done here): these bar-line routes have MULTIPLE
Create-WIP transform steps (e.g. Roasting + Mixing, or De-Seeding + Stuffing +
Enrobing), so a faithful multi-transform SFG seam would mint an intermediate
SFG#### between each consecutive transform pair. That multi-transform seam
minting is a DOCUMENTED follow-up — the SAME boundary as the routing-gap
transform seam — and is deliberately left out: this function does NOT touch the
input_code / output_code seam columns (it leaves them as the classifier /
existing ingest set them, i.e. NULL for a freshly-derived route).
"""

import logging

from app.core.helpers import BAR_LINE_LOCK
from app.modules.production.services.master_ingest import (
    STAGE_FINAL_FG,
    _route_stage_slug,
    _split_process_category,
    classify_route_steps,
)

logger = logging.getLogger(__name__)


async def _has_bar_line_routed_column(conn) -> bool:
    """True if the bom_header.bar_line_routed column (added by migration 061)
    exists. Guards a half-migrated DB: the override skips with a warning rather
    than erroring if 061 hasn't been applied."""
    return bool(await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_name = 'bom_header' AND column_name = 'bar_line_routed'
        )
        """
    ))


def _stage_for(cls: dict) -> str:
    """Derive the bom_process_route.stage slug for a classified step, matching
    ingest_jc_routing's convention: 'packing' for a terminal (Final FG) step so
    order_steps_packing_last keeps it last + EGA detection works; otherwise a
    slug of the practical operation (falling back to a slug of the step name)."""
    if cls["stage_bucket"] == STAGE_FINAL_FG:
        return "packing"
    return (_route_stage_slug(cls["practical_operation"])
            or _route_stage_slug(cls["process_name"])
            or "create_wip")


async def apply_bar_line_override(conn, *, dry_run: bool = True,
                                  only_bom_id: int | None = None,
                                  force: bool = False) -> dict:
    """(Re)derive bom_process_route from bom_header.bar_line_process for every
    bom_header carrying a non-empty bar_line_process (or just ``only_bom_id``).

    For each such header:
      * split bar_line_process on '+'  (_split_process_category),
      * classify the ordered tokens     (classify_route_steps),
      * REPLACE that bom's bom_process_route with the derived steps — mirroring
        ingest_jc_routing's REPLACE approach: DELETE the bom's existing route
        rows, then INSERT the new ones (step_number, process_name, stage,
        practical_operation, stage_bucket),
      * set bom_header.bar_line_routed = TRUE.

    Idempotent by SKIPPING already-routed headers: a header with
    bar_line_routed = TRUE is left untouched unless ``force=True``. This is
    deliberate — the DELETE-then-INSERT replacement drops the WHOLE route row,
    including any input_code / output_code SEAM columns that a later
    ``mint_sfg_seam`` run stamped on these steps. Re-routing unconditionally would
    silently un-seam those chains and orphan the minted SFGs. So by default a
    second run is a true no-op; pass ``force=True`` only to deliberately re-derive
    (e.g. after the bar_line_process string itself was corrected), accepting that
    it clears the seams and seam-minting must be re-run afterwards.

    Concurrency-safe via a transaction-scoped advisory lock; MUST run inside an
    outer transaction when writing (raises if it isn't, so the lock + the
    multi-statement REPLACE can't silently auto-commit per statement).

    dry_run=True (default): no writes — reports how many FGs would be (re)routed
    and the derived steps per FG (so the CLI can preview / sample).

    Seam columns: input_code / output_code are intentionally LEFT ALONE (NULL on
    a freshly-derived route). The multi-transform SFG seam minting for these
    routes is a documented follow-up (see module docstring) — not done here.

    Guards on the bar_line_routed column (migration 061): if absent, skips with a
    warning and returns status='schema_missing'.

    Returns a summary dict incl. per-FG counts (``fgs`` list of
    {bom_id, fg_sku_name, steps, derived[...]}).
    """
    if not await _has_bar_line_routed_column(conn):
        logger.warning(
            "Bar-line override skipped — bom_header.bar_line_routed missing; "
            "run migration 061 first"
        )
        return {"status": "schema_missing", "fgs": [], "headers": 0,
                "steps_written": 0, "dry_run": dry_run}

    if not dry_run:
        # The advisory xact-lock and the DELETE→INSERT→UPDATE per header are only
        # atomic inside an enclosing transaction; without one each statement
        # auto-commits and the lock is released immediately. Fail loudly rather
        # than corrupt routes — mirrors next_sfg_code / insert_with_pk_retry.
        if not conn.is_in_transaction():
            raise RuntimeError(
                "apply_bar_line_override(dry_run=False) must run inside a "
                "transaction (async with conn.transaction())"
            )
        # Serialise concurrent overrides — the DELETE-then-INSERT replacement is
        # not safe to interleave across transactions.
        await conn.execute("SELECT pg_advisory_xact_lock($1)", BAR_LINE_LOCK)

    # By default skip headers already routed (protects seam columns stamped by a
    # later mint_sfg_seam run, see docstring); force=True re-derives regardless.
    routed_filter = "" if force else " AND bar_line_routed IS NOT TRUE"
    if only_bom_id is not None:
        headers = await conn.fetch(
            "SELECT bom_id, fg_sku_name, bar_line_process FROM bom_header "
            "WHERE bom_id = $1 "
            "  AND bar_line_process IS NOT NULL AND btrim(bar_line_process) <> ''"
            + routed_filter,
            only_bom_id,
        )
    else:
        headers = await conn.fetch(
            "SELECT bom_id, fg_sku_name, bar_line_process FROM bom_header "
            "WHERE bar_line_process IS NOT NULL AND btrim(bar_line_process) <> ''"
            + routed_filter +
            " ORDER BY bom_id"
        )

    fgs: list[dict] = []
    steps_written = 0
    routed = 0
    skipped_empty = 0

    for h in headers:
        bom_id = h["bom_id"]
        tokens = _split_process_category(h["bar_line_process"])
        if not tokens:
            skipped_empty += 1
            continue
        cls = classify_route_steps(tokens)
        derived = [
            {
                "step_number": i,
                "process_name": c["process_name"],
                "stage": _stage_for(c),
                "practical_operation": c["practical_operation"],
                "stage_bucket": c["stage_bucket"],
            }
            for i, c in enumerate(cls, 1)
        ]
        fgs.append({
            "bom_id": bom_id,
            "fg_sku_name": h["fg_sku_name"],
            "steps": len(derived),
            "derived": derived,
        })

        if dry_run:
            continue

        # REPLACE the route: drop the existing rows, then insert the derived set.
        await conn.execute(
            "DELETE FROM bom_process_route WHERE bom_id = $1", bom_id,
        )
        for d in derived:
            await conn.execute(
                """
                INSERT INTO bom_process_route (
                    bom_id, step_number, process_name, stage,
                    practical_operation, stage_bucket
                ) VALUES ($1, $2, $3, $4, $5, $6)
                """,
                bom_id, d["step_number"], d["process_name"], d["stage"],
                d["practical_operation"], d["stage_bucket"],
            )
            steps_written += 1
        await conn.execute(
            "UPDATE bom_header SET bar_line_routed = TRUE WHERE bom_id = $1",
            bom_id,
        )
        routed += 1

    logger.info(
        "Bar-line override (%s): %d FGs, %d routed, %d steps written, %d skipped-empty",
        "DRY RUN" if dry_run else "APPLIED",
        len(fgs), routed, steps_written, skipped_empty,
    )
    return {
        "status": "ok",
        "dry_run": dry_run,
        "headers": len(fgs),
        "routed": routed,
        "steps_written": steps_written,
        "skipped_empty": skipped_empty,
        "fgs": fgs,
    }
