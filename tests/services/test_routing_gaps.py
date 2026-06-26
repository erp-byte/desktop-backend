"""Routing-Gap Resolution tests (mirrors tests/services style).

DB-FREE unit tests (always run):
  * classify_family + FAMILY_TEMPLATES for representative article names,
    asserting transform signals win over plain and the token mappings
    (Coating -> Enrobing, Packing -> Packaging) hold.
  * the suggested categories are classifier-clean: every non-blank template
    splits into tokens that master_ingest.classify_route_steps resolves
    (so apply derives the right route steps).
  * worksheet column contract.

DB-BACKED integration tests (auto-skip if Docker/asyncpg unavailable):
  Spin up a throwaway postgres:16, create the minimal bom_header /
  bom_process_route schema, and exercise:
    * get_routing_gaps excludes an already-routed article.
    * promote_articles promotes a NET-NEW article with a multi-step PC and
      derives the right ordered route steps.
    * idempotent re-apply (no duplicate routes; status routed_existing).

Runs under pytest (CI) and as a plain script (`python test_routing_gaps.py`).
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
import uuid

from app.modules.production.services import routing_gap_service as rg
from app.modules.production.services.master_ingest import (
    _split_process_category,
    classify_route_steps,
)


# ═══════════════════════════════════════════════════════════════════════════
# DB-FREE: classify_family + templates
# ═══════════════════════════════════════════════════════════════════════════

def test_classify_family_representative_names():
    cases = {
        # plain commodities / dates -> Pack-only
        "Almond Plain 75gm": rg.FAMILY_PACK_ONLY,
        "Carnival Cashew Nut 250g": rg.FAMILY_PACK_ONLY,
        "Medjoul Dates 500g": rg.FAMILY_PACK_ONLY,
        # roasted / salted -> Roasted (transform wins over the plain commodity)
        "California Almonds Roasted & Salted 200g": rg.FAMILY_ROASTED,
        "Cashew OTR 100g": rg.FAMILY_ROASTED,
        "Pistachio Salted 75gm": rg.FAMILY_ROASTED,
        # flavoured / seasoned -> Flavoured
        "Almond Flavoured Cajun Loose": rg.FAMILY_FLAVOURED,
        "Peri Peri Cashew 100g": rg.FAMILY_FLAVOURED,
        "Tandoori Almond 50g": rg.FAMILY_FLAVOURED,
        # coated peanuts -> Coated
        "Barbeque Coated Peanuts 60gm": rg.FAMILY_COATED,
        "Alpino Peanut Crackers Smokey BBQ 60g": rg.FAMILY_COATED,
        "Kracknut Masala 40g": rg.FAMILY_COATED,
        # mixes -> Mix
        "Carnival Trail Mix 200g": rg.FAMILY_MIX,
        "Fruit n Nut Combo 150g": rg.FAMILY_MIX,
        # chocolate / enrobed -> Chocolate
        "Choco Almond Dragees 100g": rg.FAMILY_CHOCOLATE,
        "Dark Chocolate Hazelnut 90g": rg.FAMILY_CHOCOLATE,
        "Enrobed Cashew 120g": rg.FAMILY_CHOCOLATE,
        # date transforms -> blank-suggestion family
        "Date Paste 1kg": rg.FAMILY_DATE_TRANSFORM,
        "Date Bar with Almond 35g": rg.FAMILY_DATE_TRANSFORM,
        "Date Syrup 500ml": rg.FAMILY_DATE_TRANSFORM,
        # no signal -> Other
        "Some Random Widget 10g": rg.FAMILY_OTHER,
        "Carnival Powder 250g": rg.FAMILY_OTHER,   # generic transform demotes to Other
    }
    for name, expected in cases.items():
        got = rg.classify_family(name)
        assert got == expected, f"{name!r}: expected {expected!r}, got {got!r}"


def test_transform_signal_wins_over_plain():
    # A roasted ALMOND must not fall through to Pack-only just because "almond"
    # is a plain commodity token — the roast signal is checked first.
    assert rg.classify_family("Almond Roasted 100g") == rg.FAMILY_ROASTED
    # A coated PEANUT is Coated, not Pack-only.
    assert rg.classify_family("Coated Peanut Cheese 50g") == rg.FAMILY_COATED
    # A chocolate cashew is Chocolate, not Pack-only.
    assert rg.classify_family("Chocolate Cashew 80g") == rg.FAMILY_CHOCOLATE


def test_family_templates_and_needs_review():
    assert rg.FAMILY_TEMPLATES[rg.FAMILY_PACK_ONLY] == "Sorting + Packaging"
    assert rg.FAMILY_TEMPLATES[rg.FAMILY_ROASTED] == "Roasting + Packaging"
    assert rg.FAMILY_TEMPLATES[rg.FAMILY_FLAVOURED] == "Roasting + Flavouring + Packaging"
    # Coating token maps to Enrobing (no "Coating" token in the classifier).
    assert rg.FAMILY_TEMPLATES[rg.FAMILY_COATED] == "Enrobing + Packaging"
    assert rg.FAMILY_TEMPLATES[rg.FAMILY_MIX] == "Blending + Packaging"
    assert rg.FAMILY_TEMPLATES[rg.FAMILY_CHOCOLATE] == "Enrobing + Packaging"
    # Date transforms are deliberately blank (needs per-item review).
    assert rg.FAMILY_TEMPLATES[rg.FAMILY_DATE_TRANSFORM] == ""
    assert rg.FAMILY_TEMPLATES[rg.FAMILY_OTHER] == "Sorting + Packaging"
    # needs_review flags
    assert rg.family_needs_review(rg.FAMILY_OTHER) is True
    assert rg.family_needs_review(rg.FAMILY_DATE_TRANSFORM) is True
    assert rg.family_needs_review(rg.FAMILY_PACK_ONLY) is False
    assert rg.family_needs_review(rg.FAMILY_ROASTED) is False


def test_templates_are_classifier_clean():
    # Every non-blank template must split into tokens the live classifier
    # resolves to a real stage_bucket (no None) — otherwise apply would derive a
    # route step the SFG/JC build can't place. This is the contract guard for the
    # Packing->Packaging and Coating->Enrobing token mappings.
    for family, pc in rg.FAMILY_TEMPLATES.items():
        if not pc:
            continue  # blank = needs review, intentionally not derivable
        steps = _split_process_category(pc)
        cls = classify_route_steps(steps)
        assert len(steps) >= 2, f"{family}: template should have >=2 steps"
        for c in cls:
            assert c["stage_bucket"] is not None, (
                f"{family}: token {c['process_name']!r} in {pc!r} does not resolve "
                f"in classify_route_steps (stage_bucket=None)"
            )
        # The last token is the terminal Final-FG (Packaging), not inline/None.
        assert cls[-1]["stage_bucket"] == "Final FG", (
            f"{family}: template {pc!r} must end in a Final-FG (Packaging) step"
        )


def test_suggest_process_category_helper():
    assert rg.suggest_process_category("Almond Roasted 100g") == "Roasting + Packaging"
    assert rg.suggest_process_category("Date Paste 1kg") == ""


def test_worksheet_columns_contract():
    assert rg.WORKSHEET_COLUMNS == [
        "article", "family", "in_all_sku",
        "suggested_process_category", "assigned_process_category",
    ]


def test_gap_union_loads_from_repo_csv():
    # The offline gap union is the 450-row CATALOG ∪ BOM_PARENT set.
    union = rg.load_gap_union()
    assert len(union) == 450, f"expected 450 gap-union rows, got {len(union)}"


# ═══════════════════════════════════════════════════════════════════════════
# DB-BACKED integration (throwaway postgres:16; auto-skip if unavailable)
# ═══════════════════════════════════════════════════════════════════════════

_PG_CONTAINER = "rg_be_test"
_PG_PORT = 55464
_DSN = f"postgresql://postgres:pg@127.0.0.1:{_PG_PORT}/candor"

_MINIMAL_SCHEMA = """
CREATE TABLE bom_header (
    bom_id            SERIAL PRIMARY KEY,
    fg_sku_name       TEXT NOT NULL,
    customer_name     TEXT,
    version           INT NOT NULL DEFAULT 1,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    entity            TEXT CHECK (entity IN ('cfpl','cdpl')),
    process_category  TEXT,
    promoted_from_gap BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE UNIQUE INDEX uq_bom_header_active_fg
    ON bom_header(fg_sku_name) WHERE is_active = TRUE;
CREATE TABLE bom_process_route (
    route_id      SERIAL PRIMARY KEY,
    bom_id        INT NOT NULL REFERENCES bom_header(bom_id),
    step_number   INT NOT NULL,
    process_name  TEXT NOT NULL,
    stage         TEXT NOT NULL,
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

    # Poll until ready (up to ~30s).
    for _ in range(60):
        try:
            asyncio.run(_probe())
            return True
        except Exception:
            time.sleep(0.5)
    return False


def _stop_pg() -> None:
    subprocess.run(["docker", "rm", "-f", _PG_CONTAINER], capture_output=True)


async def _db_scenarios() -> list[str]:
    """Run the DB-backed scenarios on a fresh schema; return a list of pass labels.
    Raises AssertionError on any failed expectation."""
    import asyncpg
    results: list[str] = []
    conn = await asyncpg.connect(_DSN)
    try:
        await conn.execute(_MINIMAL_SCHEMA)

        # ---- get_routing_gaps EXCLUDES an already-routed article ----
        # Seed a real gap-union article as already routed (header + route step).
        # Pick a name straight from the union so it would otherwise appear.
        union = rg.load_gap_union()
        routed_name = next(r["article"] for r in union
                           if rg.classify_family(r["article"]) == rg.FAMILY_PACK_ONLY)
        bom_id = await conn.fetchval(
            "INSERT INTO bom_header (fg_sku_name, process_category, promoted_from_gap) "
            "VALUES ($1, 'Sorting + Packaging', TRUE) RETURNING bom_id",
            routed_name,
        )
        await conn.execute(
            "INSERT INTO bom_process_route (bom_id, step_number, process_name, stage) "
            "VALUES ($1, 1, 'Sorting', 'sorting')", bom_id,
        )
        gaps = await rg.get_routing_gaps(conn)
        all_listed = {a["article"] for f in gaps["families"] for a in f["articles"]}
        assert routed_name not in all_listed, "already-routed article must be excluded"
        assert gaps["total"] == len(all_listed)
        assert gaps["total"] < 450, "exclusion should drop at least the routed one"
        # families[].articles[] contract shape
        sample_fam = gaps["families"][0]
        assert set(sample_fam) >= {"family", "suggested_process_category", "count",
                                   "needs_review", "articles"}
        sample_art = sample_fam["articles"][0]
        assert set(sample_art) == {"article", "in_all_sku",
                                   "current_process_category",
                                   "suggested_process_category"}
        results.append("get_routing_gaps excludes already-routed")

        # ---- promote_articles: NET-NEW multi-step PC derives ordered steps ----
        net_new = f"RG Test Roasted Cashew {uuid.uuid4().hex[:6]} 100g"
        async with conn.transaction():
            out = await rg.promote_articles(
                conn,
                [{"article": net_new, "process_category": "Roasting + Flavouring + Packaging"}],
            )
        assert out["applied"] == 1 and out["skipped"] == 0, out
        res0 = out["results"][0]
        assert res0["status"] == "promoted", res0
        assert res0["bom_id"] is not None
        steps = await conn.fetch(
            "SELECT step_number, process_name, stage FROM bom_process_route "
            "WHERE bom_id = $1 ORDER BY step_number", res0["bom_id"],
        )
        names = [s["process_name"] for s in steps]
        assert names == ["Roasting", "Flavouring", "Packaging"], names
        assert [s["step_number"] for s in steps] == [1, 2, 3]
        # header carries the PC + promoted flag
        hdr = await conn.fetchrow(
            "SELECT process_category, promoted_from_gap FROM bom_header WHERE bom_id=$1",
            res0["bom_id"],
        )
        assert hdr["process_category"] == "Roasting + Flavouring + Packaging"
        assert hdr["promoted_from_gap"] is True
        results.append("promote_articles net-new multi-step derives ordered steps")

        # ---- idempotent re-apply: no duplicate routes, routed_existing ----
        async with conn.transaction():
            out2 = await rg.promote_articles(
                conn,
                [{"article": net_new, "process_category": "Roasting + Flavouring + Packaging"}],
            )
        assert out2["applied"] == 1, out2
        assert out2["results"][0]["status"] == "routed_existing", out2["results"][0]
        cnt = await conn.fetchval(
            "SELECT count(*) FROM bom_process_route WHERE bom_id=$1", res0["bom_id"],
        )
        assert cnt == 3, f"re-apply must not duplicate routes, got {cnt}"
        results.append("promote_articles idempotent re-apply (routed_existing, no dup routes)")

        # ---- blank PC skipped; one bad row doesn't abort the rest ----
        good = f"RG Test Plain Date {uuid.uuid4().hex[:6]} 250g"
        async with conn.transaction():
            out3 = await rg.promote_articles(conn, [
                {"article": "Blank PC Article", "process_category": ""},
                {"article": good, "process_category": "Sorting + Packaging"},
            ])
        assert out3["applied"] == 1 and out3["skipped"] == 1, out3
        statuses = {r["article"]: r["status"] for r in out3["results"]}
        assert statuses["Blank PC Article"] == "skipped_no_pc"
        assert statuses[good] == "promoted"
        results.append("promote_articles skips blank PC, one bad row doesn't abort batch")

    finally:
        await conn.close()
    return results


def test_db_backed_routing_gaps():
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
    print(f"\n{passed}/{len(fns)} routing-gap tests passed")
