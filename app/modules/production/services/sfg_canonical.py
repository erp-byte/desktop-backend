"""Canonical SFG name resolver.

Maps an FG (article) name + entity to its canonical SFG name, per the SFG
canonicalization design (2026-06-22-sfg-canonicalization-design.md §4–§5.4):
free-typing the SFG is replaced by a pre-established canonical catalogue, and
the Create/Edit Job-Card SFG field auto-fills from the FG's mapping (staying
editable — free-text override allowed).

Data: ``data/sfg_canonical_map.csv`` (columns: entity, fg, fg_salegroup, case,
sfg). Rows with an empty ``sfg`` are NO_SFG cases (plain repack / hamper / RM
stock item) and resolve to None. Loaded once into a module cache.
"""

import csv
import logging
from pathlib import Path

from app.core.helpers import normalise_key

logger = logging.getLogger(__name__)

# server_replica/app/modules/production/services/sfg_canonical.py → parents[4] = server_replica
_MAP_PATH = Path(__file__).resolve().parents[4] / "data" / "sfg_canonical_map.csv"

# (entity_lower, normalised_fg) -> canonical sfg name; plus an entity-agnostic
# fallback keyed on normalised_fg alone (the catalogue is shared CFPL/CDPL, §2.2).
_by_entity_fg: dict[tuple[str, str], str] = {}
_by_fg: dict[str, str] = {}
_loaded = False


def _norm(s) -> str:
    return normalise_key(s or "").lower().strip()


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not _MAP_PATH.exists():
        logger.warning("SFG canonical map not found: %s", _MAP_PATH)
        return
    rows = 0
    with _MAP_PATH.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        next(reader, None)  # header: entity, fg, fg_salegroup, case, sfg
        for fields in reader:
            if len(fields) < 5:
                continue
            # Robust to unquoted commas inside the FG name: the trailing three
            # columns (fg_salegroup, case, sfg) are enumerated / comma-free, so
            # the FG is everything between `entity` and those three.
            entity = (fields[0] or "").strip().lower()
            sfg = (fields[-1] or "").strip()
            fg = ",".join(fields[1:-3]).strip()
            if not fg or not sfg:
                continue
            nf = _norm(fg)
            if not nf:
                continue
            _by_entity_fg[(entity, nf)] = sfg
            _by_fg.setdefault(nf, sfg)
            rows += 1
    logger.info("SFG canonical map loaded: %d FG->SFG entries from %s", rows, _MAP_PATH.name)


def resolve_canonical_sfg(fg_name, entity=None) -> str | None:
    """Canonical SFG name for an FG (article) name from the in-memory CSV cache,
    or None when the FG has no SFG (plain repack / hamper / RM) or isn't in the
    catalogue. Entity-scoped match first, then entity-agnostic fallback."""
    if not fg_name:
        return None
    _load()
    nf = _norm(fg_name)
    if not nf:
        return None
    if entity:
        hit = _by_entity_fg.get((str(entity).strip().lower(), nf))
        if hit:
            return hit
    return _by_fg.get(nf)


def _iter_csv_rows():
    """Yield (entity, fg, fg_norm, fg_salegroup, case, sfg) tuples from the CSV,
    robust to unquoted commas inside the FG name (trailing 3 cols are fixed)."""
    if not _MAP_PATH.exists():
        logger.warning("SFG canonical map not found: %s", _MAP_PATH)
        return
    with _MAP_PATH.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        next(reader, None)  # header
        for fields in reader:
            if len(fields) < 5:
                continue
            entity = (fields[0] or "").strip().lower()
            sfg = (fields[-1] or "").strip() or None
            case_label = (fields[-2] or "").strip() or None
            salegroup = (fields[-3] or "").strip() or None
            fg = ",".join(fields[1:-3]).strip()
            if not entity or not fg:
                continue
            nf = _norm(fg)
            if not nf:
                continue
            yield (entity, fg, nf, salegroup, case_label, sfg)


async def ingest_canonical_map(conn) -> dict:
    """Upsert the FG→canonical-SFG catalogue (data/sfg_canonical_map.csv) into
    the ``sfg_canonical_map`` table. Idempotent (ON CONFLICT on entity+fg_norm).
    Self-guards on a missing table so it no-ops before migration 068 lands."""
    exists = await conn.fetchval("SELECT to_regclass('sfg_canonical_map')")
    if exists is None:
        logger.warning("sfg_canonical_map ingest skipped — table missing (run 068)")
        return {"status": "schema_missing", "rows": 0}
    rows = 0
    for (entity, fg, nf, salegroup, case_label, sfg) in _iter_csv_rows():
        await conn.execute(
            """
            INSERT INTO sfg_canonical_map
                (entity, fg_name, fg_norm, fg_salegroup, case_label, sfg_name, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6, NOW())
            ON CONFLICT (entity, fg_norm) DO UPDATE SET
                fg_name      = EXCLUDED.fg_name,
                fg_salegroup = EXCLUDED.fg_salegroup,
                case_label   = EXCLUDED.case_label,
                sfg_name     = EXCLUDED.sfg_name,
                updated_at   = NOW()
            """,
            entity, fg, nf, salegroup, case_label, sfg,
        )
        rows += 1
    logger.info("SFG canonical map ingest: %d rows upserted", rows)
    return {"status": "ok", "rows": rows}


async def search_canonical_sfg(conn, q, entity=None, limit: int = 20) -> list[dict]:
    """Typeahead over the canonical SFG catalogue (``sfg_canonical_map``).

    Matches ``q`` (case-insensitive substring) against the canonical SFG name OR
    the FG (article) name, and returns DISTINCT canonical SFG suggestions. Only
    SFG-bearing rows (sfg_name NOT NULL). Entity matches rank first; direct
    sfg_name matches rank above fg-name-only matches. ``fg_count`` = how many FGs
    share that SFG (a where-used signal). Self-guards on a missing table → []."""
    term = (q or "").strip()
    if not term:
        return []
    exists = await conn.fetchval("SELECT to_regclass('sfg_canonical_map')")
    if exists is None:
        return []
    like = f"%{term}%"
    ent = str(entity).strip().lower() if entity else None
    lim = max(1, min(int(limit or 20), 50))
    rows = await conn.fetch(
        """
        SELECT sfg_name,
               COUNT(*)                       AS fg_count,
               MIN(fg_salegroup)              AS fg_salegroup,
               BOOL_OR(entity = $2)           AS entity_match,
               BOOL_OR(sfg_name ILIKE $1)     AS name_match
        FROM sfg_canonical_map
        WHERE sfg_name IS NOT NULL
          AND (sfg_name ILIKE $1 OR fg_name ILIKE $1)
        GROUP BY sfg_name
        ORDER BY entity_match DESC NULLS LAST, name_match DESC, sfg_name
        LIMIT $3
        """,
        like, ent, lim,
    )
    return [
        {
            "sfg_name": r["sfg_name"],
            "fg_count": int(r["fg_count"]),
            "fg_salegroup": r["fg_salegroup"],
        }
        for r in rows
    ]


async def resolve_canonical_sfg_db(conn, fg_name, entity=None) -> str | None:
    """DB-first canonical SFG resolve (reads ``sfg_canonical_map``), preferring an
    entity match. Falls back to the in-memory CSV resolver if the table is
    missing / the query fails, so auto-fill works pre- and post-migration."""
    if not fg_name:
        return None
    nf = _norm(fg_name)
    if not nf:
        return None
    ent = str(entity).strip().lower() if entity else None
    try:
        row = await conn.fetchrow(
            "SELECT sfg_name FROM sfg_canonical_map WHERE fg_norm = $1 "
            "ORDER BY CASE WHEN entity = $2 THEN 0 ELSE 1 END LIMIT 1",
            nf, ent,
        )
        if row is not None:
            return row["sfg_name"] or None
        # No DB row — either genuinely unmapped, or the table exists but isn't
        # populated yet (migration applied, ingest not run). Fall back to the CSV
        # (same source) so auto-fill works in that window; cheap in-memory lookup.
        return resolve_canonical_sfg(fg_name, entity)
    except Exception:  # noqa: BLE001 — table missing / transient: fall back to CSV
        return resolve_canonical_sfg(fg_name, entity)
