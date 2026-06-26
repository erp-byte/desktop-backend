"""G4 — Bar-Line Process override tests (mirrors tests/services style).

DB-FREE unit tests (always run):
  * classify_route_steps FULLY classifies BOTH bar_line_process strings — no
    step is left UNCLASSIFIED (NULL stage_bucket). This is the regression guard
    for the warm-path-re-selection bug: an unmapped token (mixing / flow) would
    leave stage_bucket NULL forever.
  * the G4 token additions: 'mixing' -> Create WIP (Blend & Form); 'flow' ->
    Final FG (terminal).

DB-BACKED integration tests (auto-skip if Docker/asyncpg unavailable):
  Spin up a throwaway postgres:16, create a minimal bom_header (incl. the 061
  bar_line_process / bar_line_routed columns) + bom_process_route, and exercise:
    * apply_bar_line_override REPLACES a bom's route from bar_line_process and
      sets bar_line_routed=TRUE.
    * dry_run=True writes nothing.
    * idempotent re-apply (same steps, converges, no duplicates).
    * schema guard: missing bar_line_routed -> status='schema_missing'.

Runs under pytest (CI) and as a plain script (`python test_bar_line.py`).
"""
from __future__ import annotations

import asyncio
import subprocess
import time

from app.modules.production.services.bar_line_service import apply_bar_line_override
from app.modules.production.services.master_ingest import (
    STAGE_CREATE_WIP,
    STAGE_FINAL_FG,
    _split_process_category,
    classify_route_steps,
)

# The two distinct bar_line_process strings (84 FGs: 82 + 2).
BAR_LINE_1 = ("Receiving + Sorting + Weighing + Roasting + Mixing + Krugger + "
              "Flow + X-ray + Monocarton + Master Carton")
BAR_LINE_2 = "De-Seeding+Sorting+Stuffing+Enrobing+Flow Wrap+Sorting+Master Carton"


# ═══════════════════════════════════════════════════════════════════════════
# DB-FREE: classify both bar_line strings with zero unclassified steps
# ═══════════════════════════════════════════════════════════════════════════

def _classify(s: str) -> list[dict]:
    return classify_route_steps(_split_process_category(s))


def test_bar_line_1_fully_classified_no_null_bucket():
    cls = _classify(BAR_LINE_1)
    assert len(cls) == 10
    unclassified = [c["process_name"] for c in cls if c["stage_bucket"] is None]
    assert unclassified == [], f"unclassified steps: {unclassified}"


def test_bar_line_2_fully_classified_no_null_bucket():
    cls = _classify(BAR_LINE_2)
    assert len(cls) == 7
    unclassified = [c["process_name"] for c in cls if c["stage_bucket"] is None]
    assert unclassified == [], f"unclassified steps: {unclassified}"


def test_mixing_token_is_create_wip_transform():
    by_name = {c["process_name"]: c for c in _classify(BAR_LINE_1)}
    mixing = by_name["Mixing"]
    assert mixing["stage_bucket"] == STAGE_CREATE_WIP
    assert mixing["practical_operation"] == "Blend & Form"
    assert mixing["produces_sfg"] is True


def test_flow_token_is_terminal_final_fg():
    by_name = {c["process_name"]: c for c in _classify(BAR_LINE_1)}
    flow = by_name["Flow"]
    assert flow["stage_bucket"] == STAGE_FINAL_FG
    assert flow["produces_sfg"] is False


def test_bar_line_2_transforms_classified():
    by_name = {c["process_name"]: c for c in _classify(BAR_LINE_2)}
    assert by_name["De-Seeding"]["stage_bucket"] == STAGE_CREATE_WIP
    assert by_name["Stuffing"]["stage_bucket"] == STAGE_CREATE_WIP
    assert by_name["Enrobing"]["stage_bucket"] == STAGE_CREATE_WIP
    assert by_name["Flow Wrap"]["stage_bucket"] == STAGE_FINAL_FG


# ═══════════════════════════════════════════════════════════════════════════
# DB-BACKED integration (throwaway postgres:16; auto-skip if unavailable)
# ═══════════════════════════════════════════════════════════════════════════

_PG_CONTAINER = "g4_be"
_PG_PORT = 55474
_DSN = f"postgresql://postgres:pg@127.0.0.1:{_PG_PORT}/candor"

# Minimal schema incl. the 061 bar_line columns the override targets.
_MINIMAL_SCHEMA = """
CREATE TABLE bom_header (
    bom_id            SERIAL PRIMARY KEY,
    fg_sku_name       TEXT NOT NULL,
    customer_name     TEXT,
    version           INT NOT NULL DEFAULT 1,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    process_category  TEXT,
    bar_line_process  TEXT,
    bar_line_routed   BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE bom_process_route (
    route_id            SERIAL PRIMARY KEY,
    bom_id              INT NOT NULL REFERENCES bom_header(bom_id),
    step_number         INT NOT NULL,
    process_name        TEXT NOT NULL,
    stage               TEXT NOT NULL,
    practical_operation TEXT,
    stage_bucket        TEXT,
    UNIQUE (bom_id, step_number)
);
"""

# Same schema WITHOUT the 061 bar_line_routed column — to exercise the guard.
_SCHEMA_NO_061 = """
CREATE TABLE bom_header (
    bom_id            SERIAL PRIMARY KEY,
    fg_sku_name       TEXT NOT NULL,
    process_category  TEXT
);
CREATE TABLE bom_process_route (
    route_id     SERIAL PRIMARY KEY,
    bom_id       INT NOT NULL REFERENCES bom_header(bom_id),
    step_number  INT NOT NULL,
    process_name TEXT NOT NULL,
    stage        TEXT NOT NULL,
    UNIQUE (bom_id, step_number)
);
"""


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


def _asyncpg_available() -> bool:
    try:
        import asyncpg  # noqa: F401
        return True
    except ImportError:
        return False


def _start_pg() -> bool:
    subprocess.run(["docker", "rm", "-f", _PG_CONTAINER], capture_output=True)
    r = subprocess.run(
        ["docker", "run", "-d", "--name", _PG_CONTAINER,
         "-e", "POSTGRES_PASSWORD=pg", "-e", "POSTGRES_DB=candor",
         "-p", f"{_PG_PORT}:5432", "postgres:16"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False
    import asyncpg

    async def _probe() -> bool:
        conn = await asyncpg.connect(_DSN)
        await conn.close()
        return True

    for _ in range(60):  # poll up to ~30s
        try:
            asyncio.run(_probe())
            return True
        except Exception:
            time.sleep(0.5)
    return False


def _stop_pg() -> None:
    subprocess.run(["docker", "rm", "-f", _PG_CONTAINER], capture_output=True)


async def _db_scenarios() -> list[str]:
    """Run the DB-backed scenarios on a fresh schema; return pass labels.
    Raises AssertionError on any failed expectation."""
    import asyncpg
    results: list[str] = []
    conn = await asyncpg.connect(_DSN)
    try:
        await conn.execute(_MINIMAL_SCHEMA)

        # Seed an FG carrying bar_line_process, with a (different) pre-existing
        # process_category-derived route to be REPLACED.
        bom_id = await conn.fetchval(
            "INSERT INTO bom_header (fg_sku_name, process_category, bar_line_process) "
            "VALUES ($1, 'Sorting + Packing', $2) RETURNING bom_id",
            "Bar-Line VA FG 30g", BAR_LINE_1,
        )
        await conn.execute(
            "INSERT INTO bom_process_route (bom_id, step_number, process_name, stage) "
            "VALUES ($1, 1, 'Sorting', 'sorting'), ($1, 2, 'Packing', 'packing')",
            bom_id,
        )

        # ---- dry_run writes nothing ----
        dry = await apply_bar_line_override(conn, dry_run=True)
        assert dry["status"] == "ok"
        assert dry["headers"] == 1, dry
        assert dry["fgs"][0]["steps"] == 10, dry["fgs"][0]
        # route unchanged + flag still FALSE
        n_steps = await conn.fetchval(
            "SELECT count(*) FROM bom_process_route WHERE bom_id=$1", bom_id)
        assert n_steps == 2, "dry run must not write"
        routed_flag = await conn.fetchval(
            "SELECT bar_line_routed FROM bom_header WHERE bom_id=$1", bom_id)
        assert routed_flag is False, "dry run must not set bar_line_routed"
        results.append("apply_bar_line_override dry_run writes nothing")

        # ---- apply: REPLACE route + set bar_line_routed ----
        async with conn.transaction():
            applied = await apply_bar_line_override(conn, dry_run=False)
        assert applied["status"] == "ok" and applied["routed"] == 1, applied
        assert applied["steps_written"] == 10, applied
        steps = await conn.fetch(
            "SELECT step_number, process_name, stage, stage_bucket "
            "FROM bom_process_route WHERE bom_id=$1 ORDER BY step_number", bom_id)
        names = [s["process_name"] for s in steps]
        assert names == _split_process_category(BAR_LINE_1), names
        # NO step left unclassified (the warm-path-re-selection guard)
        assert all(s["stage_bucket"] is not None for s in steps), \
            "every derived step must carry a stage_bucket"
        # terminal step stage = 'packing' (so order_steps_packing_last keeps it last)
        assert steps[-1]["stage"] == "packing", steps[-1]
        routed_flag = await conn.fetchval(
            "SELECT bar_line_routed FROM bom_header WHERE bom_id=$1", bom_id)
        assert routed_flag is True
        results.append("apply_bar_line_override replaces route + sets bar_line_routed")

        # ---- default re-apply is a NO-OP: already-routed headers are SKIPPED
        #      (protects any seam columns a later mint_sfg_seam stamped) ----
        async with conn.transaction():
            again = await apply_bar_line_override(conn, dry_run=False)
        assert again["routed"] == 0 and again["headers"] == 0, again
        cnt = await conn.fetchval(
            "SELECT count(*) FROM bom_process_route WHERE bom_id=$1", bom_id)
        assert cnt == 10, f"no-op re-apply must leave the route intact, got {cnt}"
        results.append("apply_bar_line_override default re-apply skips already-routed (no-op)")

        # ---- force=True re-apply DOES re-derive, still no duplicate steps ----
        async with conn.transaction():
            forced = await apply_bar_line_override(conn, dry_run=False, force=True)
        assert forced["routed"] == 1, forced
        cnt2 = await conn.fetchval(
            "SELECT count(*) FROM bom_process_route WHERE bom_id=$1", bom_id)
        assert cnt2 == 10, f"force re-apply must not duplicate steps, got {cnt2}"
        names2 = [r["process_name"] for r in await conn.fetch(
            "SELECT process_name FROM bom_process_route WHERE bom_id=$1 "
            "ORDER BY step_number", bom_id)]
        assert names2 == _split_process_category(BAR_LINE_1), names2
        results.append("apply_bar_line_override force=True re-derives (no dup steps)")

        # ---- only_bom_id scoping: a SECOND bar-line FG is untouched ----
        bom_id2 = await conn.fetchval(
            "INSERT INTO bom_header (fg_sku_name, bar_line_process) "
            "VALUES ($1, $2) RETURNING bom_id", "Bar-Line VA FG2 50g", BAR_LINE_2)
        # force=True because bom_id is already routed from the block above; the
        # point here is that only_bom_id leaves the OTHER FG (bom_id2) untouched.
        async with conn.transaction():
            scoped = await apply_bar_line_override(conn, dry_run=False, only_bom_id=bom_id, force=True)
        assert scoped["routed"] == 1 and scoped["headers"] == 1, scoped
        flag2 = await conn.fetchval(
            "SELECT bar_line_routed FROM bom_header WHERE bom_id=$1", bom_id2)
        assert flag2 is False, "only_bom_id must not touch other FGs"
        results.append("apply_bar_line_override only_bom_id scoping")

        # ---- second FG (string 2) routes cleanly too ----
        async with conn.transaction():
            r2 = await apply_bar_line_override(conn, dry_run=False, only_bom_id=bom_id2)
        assert r2["steps_written"] == 7, r2
        s2 = await conn.fetch(
            "SELECT stage_bucket FROM bom_process_route WHERE bom_id=$1", bom_id2)
        assert all(r["stage_bucket"] is not None for r in s2)
        results.append("apply_bar_line_override routes bar_line string 2 (no NULL bucket)")

    finally:
        await conn.close()

    # ---- schema guard: a DB WITHOUT bar_line_routed skips with a warning ----
    conn = await asyncpg.connect(_DSN)
    try:
        await conn.execute("DROP TABLE bom_process_route; DROP TABLE bom_header;")
        await conn.execute(_SCHEMA_NO_061)
        await conn.execute(
            "INSERT INTO bom_header (fg_sku_name) VALUES ('x')")
        guard = await apply_bar_line_override(conn, dry_run=True)
        assert guard["status"] == "schema_missing", guard
        results.append("apply_bar_line_override guards on missing bar_line_routed (061)")
    finally:
        await conn.close()
    return results


def test_db_backed_bar_line_override():
    if not _asyncpg_available():
        print("SKIP DB tests: asyncpg not importable")
        return
    if not _docker_available():
        print("SKIP DB tests: docker not available")
        return
    if not _start_pg():
        _stop_pg()
        print("SKIP DB tests: could not start postgres:16")
        return
    try:
        labels = asyncio.run(_db_scenarios())
        for lbl in labels:
            print(f"  ok  [db] {lbl}")
    finally:
        _stop_pg()


if __name__ == "__main__":  # plain-script runner (no pytest needed)
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  ok  {fn.__name__}")
    print(f"\n{passed}/{len(fns)} bar-line (G4) tests passed")
