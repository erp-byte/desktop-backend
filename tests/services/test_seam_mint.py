"""Phase 9 — SFG seam minting tests (mirrors tests/services style).

DB-FREE unit tests (always run):
  * _base_recipe strips representative pack / size / marketing / id tokens so two
    pack variants of a recipe collapse to ONE family (the dedup foundation).

DB-BACKED integration tests (auto-skip if Docker / asyncpg unavailable):
  Spin up a throwaway postgres:16, create a minimal all_sku (incl. 051 sfg_code) +
  sfg_attributes + bom_header + bom_process_route, and PROVE the dedup contract:
    (a) two FGs with the SAME base_recipe + operation share ONE minted SFG
        (minted == 1, NOT 2);
    (b) a DIFFERENT base_recipe -> a different (new) SFG;
    (c) an EXISTING catalogue SFG matching the key is REUSED (minted=0, reused=1);
    (d) a re-run mints 0 (idempotent);
    (e) the seam is stamped on the producer (output_code/output_kind) and the
        consumer (input_code/input_kind) for a 2-transform chain;
    (f) (covered DB-free) _base_recipe strips representative pack tokens.

Runs under pytest (CI) and as a plain script (`python test_seam_mint.py`).
"""
from __future__ import annotations

import asyncio
import subprocess
import time

from app.modules.production.services.seam_mint_service import (
    _base_recipe,
    _dedup_key,
    mint_sfg_seam_for_chains,
)


# ═══════════════════════════════════════════════════════════════════════════
# DB-FREE: _base_recipe pack-token stripping  (assertion (f))
# ═══════════════════════════════════════════════════════════════════════════

def test_base_recipe_strips_pack_tokens():
    cases = {
        "Almond Bar 30g": "almond bar",
        "Almond Bar 50g": "almond bar",            # pack variant -> same family
        "Roasted Cashew 100 gm": "roasted cashew",
        "Roasted Cashew 1kg": "roasted cashew",
        "Date Bar 48 gms": "date bar",
        "Trail Mix 250G": "trail mix",
        "Cashew Brittle 500 Gm": "cashew brittle",
        "Peanut Chikki 1*1": "peanut chikki",
        "Mixed Nuts Pack of 12": "mixed nuts",
        "Choco Date 100gm (1023456)": "choco date",
        "Granola 1L": "granola",
        "Fig Bar 35g": "fig bar",
    }
    for raw, expected in cases.items():
        assert _base_recipe(raw) == expected, f"{raw!r} -> {_base_recipe(raw)!r} (want {expected!r})"


def test_base_recipe_pack_variants_collapse():
    # Two pack sizes of the same recipe must produce the same base_recipe.
    assert _base_recipe("Almond Bar 30g") == _base_recipe("Almond Bar 50g")
    assert _base_recipe("Almond Bar 30g") != _base_recipe("Cashew Bar 30g")


def test_base_recipe_conservative_keeps_unknown_tokens():
    # No pack/size token -> name preserved (lowercased). Conservative: don't
    # over-strip and merge distinct recipes.
    assert _base_recipe("Honey Roasted Almonds") == "honey roasted almonds"
    assert _base_recipe("") == ""
    assert _base_recipe(None) == ""


def test_dedup_key_pack_variants_match():
    k1 = _dedup_key("Almond Bar 30g", "Blend & Form")
    k2 = _dedup_key("Almond Bar 50g", "Blend & Form")
    assert k1 == k2
    # different operation -> different key
    assert _dedup_key("Almond Bar 30g", "Roasting") != k1


# ═══════════════════════════════════════════════════════════════════════════
# DB-BACKED integration (throwaway postgres:16; auto-skip if unavailable)
# ═══════════════════════════════════════════════════════════════════════════

_PG_CONTAINER = "p9_be"
_PG_PORT = 55476
_DSN = f"postgresql://postgres:pg@127.0.0.1:{_PG_PORT}/candor"

# Minimal schema: all_sku (incl. 051 sfg_code + sfg_origin), sfg_attributes (056),
# bom_header, bom_process_route (incl. 050 seam columns). Mirrors the real schema
# closely enough to exercise the service end-to-end.
_MINIMAL_SCHEMA = """
CREATE TABLE all_sku (
    sku_id      BIGINT PRIMARY KEY,
    sfg_code    TEXT,
    particulars TEXT NOT NULL,
    item_type   TEXT,
    item_group  TEXT,
    sale_group  TEXT,
    sfg_origin  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX uq_all_sku_sfg_code ON all_sku(sfg_code) WHERE sfg_code IS NOT NULL;

CREATE TABLE sfg_attributes (
    sfg_code             TEXT PRIMARY KEY,
    base_recipe          TEXT,
    create_wip_operation TEXT,
    sfg_origin           TEXT,
    item_group           TEXT,
    consumed_by_fgs_count INT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE bom_header (
    bom_id      SERIAL PRIMARY KEY,
    fg_sku_name TEXT NOT NULL,
    entity      TEXT
);
CREATE TABLE bom_process_route (
    route_id            SERIAL PRIMARY KEY,
    bom_id              INT NOT NULL REFERENCES bom_header(bom_id),
    step_number         INT NOT NULL,
    process_name        TEXT NOT NULL,
    stage               TEXT NOT NULL,
    practical_operation TEXT,
    stage_bucket        TEXT,
    input_kind          TEXT,
    output_kind         TEXT,
    input_code          TEXT,
    output_code         TEXT,
    UNIQUE (bom_id, step_number)
);
"""

# Same minus the 051 sfg_code column — exercises the schema guard.
_SCHEMA_NO_051 = """
CREATE TABLE all_sku (
    sku_id      BIGINT PRIMARY KEY,
    particulars TEXT NOT NULL,
    item_type   TEXT
);
CREATE TABLE bom_header (bom_id SERIAL PRIMARY KEY, fg_sku_name TEXT NOT NULL);
CREATE TABLE bom_process_route (
    route_id     SERIAL PRIMARY KEY,
    bom_id       INT NOT NULL REFERENCES bom_header(bom_id),
    step_number  INT NOT NULL,
    process_name TEXT NOT NULL,
    stage        TEXT NOT NULL,
    stage_bucket TEXT,
    output_code  TEXT,
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


async def _seed_chain(conn, fg_name: str, steps: list[dict]) -> int:
    """Create a bom_header + its ordered bom_process_route steps. Each step dict:
    {process_name, stage_bucket, practical_operation?}. Returns bom_id."""
    bom_id = await conn.fetchval(
        "INSERT INTO bom_header (fg_sku_name) VALUES ($1) RETURNING bom_id", fg_name)
    for i, s in enumerate(steps, 1):
        await conn.execute(
            "INSERT INTO bom_process_route "
            "(bom_id, step_number, process_name, stage, practical_operation, stage_bucket) "
            "VALUES ($1,$2,$3,$4,$5,$6)",
            bom_id, i, s["process_name"], s.get("stage", "create_wip"),
            s.get("practical_operation"), s["stage_bucket"],
        )
    return bom_id


async def _db_scenarios() -> list[str]:
    import asyncpg
    results: list[str] = []
    conn = await asyncpg.connect(_DSN)
    try:
        await conn.execute(_MINIMAL_SCHEMA)

        CW = "Create WIP"

        # ── (a) two FGs SAME base_recipe + operation share ONE minted SFG ──────
        # "Almond Bar 30g" and "Almond Bar 50g" both Blend & Form -> one SFG.
        b1 = await _seed_chain(conn, "Almond Bar 30g", [
            {"process_name": "Blend & Form", "practical_operation": "Blend & Form",
             "stage_bucket": CW},
            {"process_name": "Packing", "stage": "packing", "stage_bucket": "Final FG"},
        ])
        b2 = await _seed_chain(conn, "Almond Bar 50g", [
            {"process_name": "Blend & Form", "practical_operation": "Blend & Form",
             "stage_bucket": CW},
            {"process_name": "Packing", "stage": "packing", "stage_bucket": "Final FG"},
        ])
        async with conn.transaction():
            res = await mint_sfg_seam_for_chains(conn, dry_run=False)
        assert res["status"] == "ok", res
        assert res["minted"] == 1, f"(a) two same-recipe FGs must mint ONE SFG, got {res['minted']}"
        assert res["reused"] == 1, f"(a) second FG must REUSE, got reused={res['reused']}"
        # Both producer steps point at the SAME sfg_code.
        c1 = await conn.fetchval(
            "SELECT output_code FROM bom_process_route WHERE bom_id=$1 AND step_number=1", b1)
        c2 = await conn.fetchval(
            "SELECT output_code FROM bom_process_route WHERE bom_id=$1 AND step_number=1", b2)
        assert c1 is not None and c1 == c2, f"(a) shared SFG mismatch: {c1!r} vs {c2!r}"
        # Exactly ONE new all_sku SFG row was created for this family.
        n_sfg = await conn.fetchval(
            "SELECT count(*) FROM all_sku WHERE item_type='sfg' AND sfg_code=$1", c1)
        assert n_sfg == 1, n_sfg
        results.append("(a) two same base_recipe+op FGs share ONE minted SFG (minted=1, not 2)")

        # ── (e) seam stamped on producer + consumer for a 2-transform chain ────
        # De-Seeding (CW) -> Stuffing (CW) -> Packing (Final). Step1 produces SFG_1
        # consumed by step2; step2 produces SFG_2 consumed by step3.
        b3 = await _seed_chain(conn, "Stuffed Date Roll", [
            {"process_name": "De-Seeding", "practical_operation": "De-Seed",
             "stage_bucket": CW},
            {"process_name": "Stuffing", "practical_operation": "Stuff",
             "stage_bucket": CW},
            {"process_name": "Packing", "stage": "packing", "stage_bucket": "Final FG"},
        ])
        async with conn.transaction():
            res_e = await mint_sfg_seam_for_chains(conn, dry_run=False, only_bom_id=b3)
        assert res_e["minted"] == 2, f"(e) 2-transform chain mints 2 SFGs, got {res_e['minted']}"
        assert res_e["steps_seamed"] == 2, res_e
        rows = await conn.fetch(
            "SELECT step_number, input_code, input_kind, output_code, output_kind "
            "FROM bom_process_route WHERE bom_id=$1 ORDER BY step_number", b3)
        s1, s2, s3 = rows
        # producer 1 -> consumer 2
        assert s1["output_code"] and s1["output_kind"] == "SFG", dict(s1)
        assert s2["input_code"] == s1["output_code"] and s2["input_kind"] == "SFG", \
            f"(e) step2 must consume step1's SFG: {dict(s2)}"
        # producer 2 -> consumer 3
        assert s2["output_code"] and s2["output_kind"] == "SFG", dict(s2)
        assert s3["input_code"] == s2["output_code"] and s3["input_kind"] == "SFG", \
            f"(e) step3 must consume step2's SFG: {dict(s3)}"
        # the two minted SFGs are DISTINCT
        assert s1["output_code"] != s2["output_code"], "(e) chain SFGs must differ"
        results.append("(e) seam stamped producer(output)+consumer(input) across a 2-transform chain")

        # ── (b) DIFFERENT base_recipe -> a DIFFERENT SFG ──────────────────────
        b4 = await _seed_chain(conn, "Cashew Brittle 100g", [
            {"process_name": "Blend & Form", "practical_operation": "Blend & Form",
             "stage_bucket": CW},
            {"process_name": "Packing", "stage": "packing", "stage_bucket": "Final FG"},
        ])
        async with conn.transaction():
            res_b = await mint_sfg_seam_for_chains(conn, dry_run=False, only_bom_id=b4)
        assert res_b["minted"] == 1, f"(b) new recipe mints a fresh SFG, got {res_b['minted']}"
        c4 = await conn.fetchval(
            "SELECT output_code FROM bom_process_route WHERE bom_id=$1 AND step_number=1", b4)
        assert c4 != c1, f"(b) different base_recipe must NOT reuse Almond Bar's SFG ({c4} == {c1})"
        results.append("(b) different base_recipe -> different SFG")

        # ── (c) an EXISTING catalogue SFG matching the key is REUSED ───────────
        # Pre-seed a catalogue SFG for ("roasted peanut","Roasting") then route an
        # FG whose chain hits that exact key. Expect minted=0, reused=1.
        await conn.execute(
            "INSERT INTO all_sku (sku_id, sfg_code, particulars, item_type, sale_group) "
            "VALUES (90000001, 'SFG9001', 'Roasted Peanut WIP', 'sfg', 'wip')")
        await conn.execute(
            "INSERT INTO sfg_attributes (sfg_code, base_recipe, create_wip_operation, sfg_origin) "
            "VALUES ('SFG9001', 'roasted peanut', 'Roasting', 'DERIVED_INLINE')")
        b5 = await _seed_chain(conn, "Roasted Peanut 200g", [
            {"process_name": "Roasting", "practical_operation": "Roasting",
             "stage_bucket": CW},
            {"process_name": "Packing", "stage": "packing", "stage_bucket": "Final FG"},
        ])
        async with conn.transaction():
            res_c = await mint_sfg_seam_for_chains(conn, dry_run=False, only_bom_id=b5)
        assert res_c["minted"] == 0, f"(c) existing SFG must be reused, not minted: {res_c}"
        assert res_c["reused"] == 1, res_c
        c5 = await conn.fetchval(
            "SELECT output_code FROM bom_process_route WHERE bom_id=$1 AND step_number=1", b5)
        assert c5 == "SFG9001", f"(c) must reuse the existing SFG9001, got {c5}"
        results.append("(c) existing catalogue SFG matching the key is REUSED (minted=0, reused=1)")

        # ── (d) a full re-run mints 0 (idempotent) ────────────────────────────
        async with conn.transaction():
            res_d = await mint_sfg_seam_for_chains(conn, dry_run=False)
        assert res_d["minted"] == 0, f"(d) re-run must mint 0, got {res_d['minted']}"
        assert res_d["reused"] == 0, f"(d) re-run must reuse 0 (all already seamed), got {res_d}"
        assert res_d["steps_seamed"] == 0, res_d
        results.append("(d) full re-run mints 0 (idempotent)")

        # ── minted-row provenance: SEAM_MINTED origin on all_sku + sfg_attributes ─
        origin_all_sku = await conn.fetchval(
            "SELECT sfg_origin FROM all_sku WHERE sfg_code=$1", c1)
        origin_attrs = await conn.fetchval(
            "SELECT sfg_origin FROM sfg_attributes WHERE sfg_code=$1", c1)
        assert origin_all_sku == "SEAM_MINTED", origin_all_sku
        assert origin_attrs == "SEAM_MINTED", origin_attrs
        results.append("minted SFGs tagged sfg_origin=SEAM_MINTED (all_sku + sfg_attributes)")

        # ── dry_run writes nothing but reports exact dedup counts ─────────────
        b6 = await _seed_chain(conn, "Sesame Bar 40g", [
            {"process_name": "Blend & Form", "practical_operation": "Blend & Form",
             "stage_bucket": CW},
            {"process_name": "Packing", "stage": "packing", "stage_bucket": "Final FG"},
        ])
        b7 = await _seed_chain(conn, "Sesame Bar 60g", [
            {"process_name": "Blend & Form", "practical_operation": "Blend & Form",
             "stage_bucket": CW},
            {"process_name": "Packing", "stage": "packing", "stage_bucket": "Final FG"},
        ])
        async with conn.transaction():
            dry = await mint_sfg_seam_for_chains(conn, dry_run=True)
        assert dry["minted"] == 1 and dry["reused"] == 1, \
            f"dry_run dedup must match apply: {dry}"
        # nothing written
        unseamed = await conn.fetchval(
            "SELECT count(*) FROM bom_process_route "
            "WHERE bom_id IN ($1,$2) AND step_number=1 AND output_code IS NULL", b6, b7)
        assert unseamed == 2, "dry_run must not write seams"
        results.append("dry_run reports exact dedup (minted=1,reused=1) and writes nothing")

    finally:
        await conn.close()

    # ── schema guard: a DB WITHOUT all_sku.sfg_code skips gracefully ──────────
    conn = await asyncpg.connect(_DSN)
    try:
        await conn.execute("DROP TABLE bom_process_route; DROP TABLE bom_header; "
                           "DROP TABLE sfg_attributes; DROP TABLE all_sku;")
        await conn.execute(_SCHEMA_NO_051)
        await conn.execute("INSERT INTO bom_header (fg_sku_name) VALUES ('x')")
        guard = await mint_sfg_seam_for_chains(conn, dry_run=True)
        assert guard["status"] == "schema_missing", guard
        results.append("guards on missing all_sku.sfg_code (051) -> schema_missing")
    finally:
        await conn.close()
    return results


def test_db_backed_seam_mint():
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
    print(f"\n{passed}/{len(fns)} seam-mint (Phase 9) tests passed")
