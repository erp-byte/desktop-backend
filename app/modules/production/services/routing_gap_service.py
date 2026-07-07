"""Routing-Gap Resolution — close the remaining SFG reconciliation-gap FG articles.

Slice-7 (scripts/promote_fg_master_gaps.py) auto-promoted the ~108 cleanly-ruleable
"plain dates" gap articles into FG Master (a bom_header carrying a process_category
+ derived bom_process_route steps, stamped promoted_from_gap). The remaining ~342
gap FGs carry NO business rule and NO routing source, so they cannot be auto-routed
safely — a roasted / coated / flavoured product is NOT "Sorting + Packing".

This service is the human-in-the-loop tool that closes the rest:
  * classify each remaining gap article into a FAMILY (by name signals) and
    SUGGEST a process_category template for it;
  * surface that as a grouped gap list + a downloadable worksheet so production
    can review and assign the real category;
  * APPLY the confirmed assignments — upserting bom_header + deriving routes via
    the EXACT same primitive the Slice-7 script uses (promote_one, shared here).

The two reconciliation gaps it covers (gap_flags in the offline
docs/.../Article_Master_FINAL.csv):
  * 403  CATALOG_FG_NOT_IN_FG_MASTER  — an all_sku FG with no Process Category
         (net-new: no bom_header yet) -> promote_one INSERTs the header.
  * 238  BOM_PARENT_NO_ROUTING        — a BOM parent (recipe exists) with no
         routing (header exists, route empty) -> promote_one routes it in place.

HONESTY ON THE SUGGESTIONS (read before trusting them):
  * The family templates are HEURISTIC suggestions from the article NAME, NOT an
    authoritative routing source. Production confirms the real category via the
    apply assignments. needs_review=TRUE flags families where the suggestion is a
    default ("Other") or deliberately blank ("Date Products (transform)").
  * "Other" defaults to pack-only ("Sorting + Packaging") but is marked
    needs_review — a name with no recognised signal MIGHT still be a transform.
  * "Date Products (transform)" (paste / syrup / bar / bites / ball / powder)
    gets a BLANK suggestion: these need a real multi-step routing (bar-forming,
    enrobing, milling) chosen per-item, not a guessed template.

TOKEN NOTE (classifier compatibility):
  master_ingest.classify_route_steps splits process_category on '+' and resolves
  each token against its maps (_TRANSFORM_OPS / _TERMINAL_TOKENS / _INLINE_TOKENS).
  The templates here use ONLY tokens that resolve cleanly:
    * "Packaging" — the terminal/Final-FG token. NOTE: "Packing" does NOT resolve
      (only "Packaging" is in _TERMINAL_TOKENS), so every template ends in
      "Packaging", not "Packing".
    * "Sorting"   — inline token.
    * "Roasting", "Flavouring", "Blending", "Enrobing" — Create-WIP transform ops.
    * COATING MAPPING: there is no "Coating" token in _TRANSFORM_OPS, so the
      "Coated Peanuts" family maps to the nearest recognised transform op,
      "Enrobing" (-> "Enrobe / Choco-Coat"). Coating peanuts (kracknut / crackers)
      is the same enrobe/coat operation class.
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path

# Reuse the live route-derivation primitives (DON'T reinvent): the same
# split-on-'+' the ingest + Slice-7 script use, and the shared promote_one upsert.
from app.modules.production.services.master_ingest import _split_process_category

# ── source data (the offline gap union) ──────────────────────────────────────
# routing_gap_service.py is at app/modules/production/services/, so parents[4] is
# the server_replica root — same anchor master_ingest uses for its plug files.
_REPO_ROOT = Path(__file__).resolve().parents[4]
ARTICLE_MASTER_CSV = (
    _REPO_ROOT / "docs" / "flows" / "Candor_SFG_JobCard_Integration_2026-06-14"
    / "Article_Master_FINAL.csv"
)

# The two gap flags this feature clears (substring match on the multi-valued,
# '; '-separated gap_flags column).
GAP_CATALOG = "CATALOG_FG_NOT_IN_FG_MASTER"
GAP_BOM = "BOM_PARENT_NO_ROUTING"


# ── FAMILY CLASSIFIER + TEMPLATES ────────────────────────────────────────────
# family -> suggested process_category. Tokens are classifier-clean (see module
# docstring TOKEN NOTE). A blank "" means "needs per-item review" (no safe guess).
FAMILY_PACK_ONLY = "Pack-only"
FAMILY_ROASTED = "Roasted/Salted Nuts"
FAMILY_FLAVOURED = "Flavoured/Seasoned Nuts"
FAMILY_COATED = "Coated Peanuts"
FAMILY_MIX = "Trail/Nut/Seed Mix"
FAMILY_CHOCOLATE = "Chocolate/Enrobed"
FAMILY_DATE_TRANSFORM = "Date Products (transform)"
FAMILY_OTHER = "Other"

FAMILY_TEMPLATES: dict[str, str] = {
    FAMILY_PACK_ONLY:      "Sorting + Packaging",
    FAMILY_ROASTED:        "Roasting + Packaging",
    FAMILY_FLAVOURED:      "Roasting + Flavouring + Packaging",
    # "Coating" has no classifier token -> mapped to "Enrobing" (Enrobe/Choco-Coat).
    FAMILY_COATED:         "Enrobing + Packaging",
    FAMILY_MIX:            "Blending + Packaging",
    FAMILY_CHOCOLATE:      "Enrobing + Packaging",
    # Transform date products need a real per-item routing — no safe template.
    FAMILY_DATE_TRANSFORM: "",
    # Default = pack-only, but flagged needs_review (see _NEEDS_REVIEW_FAMILIES).
    FAMILY_OTHER:          "Sorting + Packaging",
}

# Families whose suggestion is a guess production MUST confirm.
_NEEDS_REVIEW_FAMILIES = frozenset({FAMILY_OTHER, FAMILY_DATE_TRANSFORM})

# ── name-signal regexes (ORDERED so transform signals win over plain) ────────
# Each uses word-ish boundaries / explicit phrases so a substring can't false-match.
_RE_CHOCOLATE = re.compile(r"\b(chocolate|choco|enrob(?:e|ed)?|dipped)\b", re.IGNORECASE)
_RE_COATED = re.compile(r"(coated\s+peanut|kracknut|cracker|alpino)", re.IGNORECASE)
_RE_FLAVOURED = re.compile(
    r"\b(flavou?red|masala|peri|bbq|cajun|paprika|tandoori|smoked)\b", re.IGNORECASE
)
# Roasted/Salted: roasted/rstd/oven-roasted/OTR, or "roast"+"salt" co-present.
_RE_ROASTED = re.compile(r"\b(roasted|rstd|otr|oven\s+roasted|roast)\b", re.IGNORECASE)
_RE_SALTED = re.compile(r"\bsalt(?:ed)?\b", re.IGNORECASE)
_RE_MIX = re.compile(
    r"(trail\s*mix|nut\s*mix|seeds?\s*&\s*berries|fruit\s*[n&]\s*nut)", re.IGNORECASE
)
# A "date" name with a transform signal -> Date Products (transform).
_RE_DATE = re.compile(r"\bdate", re.IGNORECASE)
_RE_DATE_TRANSFORM = re.compile(
    r"\b(paste|syrup|bar|bites?|ball|powder)\b", re.IGNORECASE
)
# A generic transform signal that turns ANY plain-nut/fruit name into "Other +
# needs_review" rather than auto-bucketing it as pack-only (powder/paste/glazed/
# stuffed/caramel/honey-roast etc. all need a real route, not Sorting+Packaging).
_RE_GENERIC_TRANSFORM = re.compile(
    r"\b(powder|paste|syrup|glazed|stuffed|caramel|honey\s*roast|"
    r"dragee|brittle|toffee|candied)\b", re.IGNORECASE
)
# Plain whole nut / dried-fruit / seed commodity tokens. A name carrying one of
# these and NO transform signal is genuine pack-only (contract: plain nuts /
# plain fruit / plain dates -> Sorting + Packaging). Word-boundary so it can't
# false-match inside another word.
_RE_PLAIN_COMMODITY = re.compile(
    r"\b(almond|almonds|cashew|cashews|pista|pistachio|pistachios|walnut|walnuts|"
    r"pecan|pecans|macadamia|hazelnut|hazelnuts|pine\s*nut|peanut|peanuts|"
    r"raisin|raisins|kishmish|anjeer|apricot|prune|prunes|fig|figs|cranberry|"
    r"blueberry|blackcurrant|currant|makhana|chia|seed|seeds|abjosh|mewa|"
    r"sunflower|pumpkin|melon|berry|berries|dry\s*fruit|dried\s*fruit|nut|nuts)\b",
    re.IGNORECASE,
)


def classify_family(article_name: str) -> str:
    """Classify an FG article NAME into a routing family (see FAMILY_TEMPLATES).

    HEURISTIC, name-based — a SUGGESTION, not authoritative. Order matters:
    transform signals (chocolate / coated / flavoured / roasted / mix /
    date-transform) are checked BEFORE the plain pack-only fallback, so a
    "Roasted & Salted Almond" is Roasted, not Pack-only. Returns FAMILY_OTHER
    (default pack-only, needs_review) when no signal matches.
    """
    name = article_name or ""

    # 1. Date products with a transform signal need a real per-item route (blank).
    if _RE_DATE.search(name) and _RE_DATE_TRANSFORM.search(name):
        return FAMILY_DATE_TRANSFORM
    # 2. Chocolate / enrobed (strong transform signal).
    if _RE_CHOCOLATE.search(name):
        return FAMILY_CHOCOLATE
    # 3. Coated peanuts (kracknut / crackers / alpino).
    if _RE_COATED.search(name):
        return FAMILY_COATED
    # 4. Flavoured / seasoned.
    if _RE_FLAVOURED.search(name):
        return FAMILY_FLAVOURED
    # 5. Roasted / salted (roast OR salt signal -> roasting route).
    if _RE_ROASTED.search(name) or _RE_SALTED.search(name):
        return FAMILY_ROASTED
    # 6. Trail / nut / seed mixes.
    if _RE_MIX.search(name):
        return FAMILY_MIX
    # 7. Plain pack-only: an explicit date / nut / dried-fruit / seed commodity
    #    name with NO transform signal. (A plain "date" article fell through the
    #    date-transform check at step 1, so it is whole dates in a retail pack;
    #    plain nuts/fruit are caught by the commodity tokens.) A generic transform
    #    signal (powder/paste/glazed/caramel/...) demotes it to Other+review even
    #    if it also names a commodity.
    if not _RE_GENERIC_TRANSFORM.search(name) and (
            _RE_DATE.search(name) or _RE_PLAIN_COMMODITY.search(name)):
        return FAMILY_PACK_ONLY
    # 8. Anything else — default to pack-only but flag for review.
    return FAMILY_OTHER


def suggest_process_category(article_name: str) -> str:
    """Convenience: the suggested process_category for an article (may be "")."""
    return FAMILY_TEMPLATES.get(classify_family(article_name), "")


def family_needs_review(family: str) -> bool:
    return family in _NEEDS_REVIEW_FAMILIES


# ── CSV load + entity mapping ────────────────────────────────────────────────

def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("true", "1", "yes", "y", "t")


def _csv_entity(val: str | None) -> str | None:
    """Map the CSV catalog_entity to a bom_header.entity value (CHECK IN
    ('cfpl','cdpl')). 'cfpl+cdpl' / '' -> None (NULL passes the CHECK; a generic,
    entity-agnostic header). Mirrors the Slice-7 script's mapping exactly."""
    v = (val or "").strip().lower()
    return v if v in ("cfpl", "cdpl") else None


def load_gap_union(csv_path: Path | None = None) -> list[dict]:
    """Rows whose gap_flags contain EITHER target gap (the 450-row union)."""
    path = Path(csv_path) if csv_path else ARTICLE_MASTER_CSV
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return [
        r for r in rows
        if GAP_CATALOG in (r.get("gap_flags") or "") or GAP_BOM in (r.get("gap_flags") or "")
    ]


# ── already-routed set (so the ~108 promoted + any since-routed drop off) ─────

async def _already_routed_names(conn) -> set[str]:
    """Names of bom_header rows that now have BOTH a process_category AND at
    least one bom_process_route step — i.e. articles that have LEFT the gap.

    These must be EXCLUDED from the gap list so the Slice-7 ~108 (and anything
    routed since) don't reappear as outstanding work. Uses the live fg_routing_status
    view when present (the DB agent's view), else the underlying tables directly.
    """
    view_exists = await conn.fetchval("SELECT to_regclass('fg_routing_status')") is not None
    if view_exists:
        rows = await conn.fetch(
            """
            SELECT fg_sku_name FROM fg_routing_status
             WHERE has_routing
               AND COALESCE(NULLIF(process_category, ''), NULL) IS NOT NULL
            """
        )
    else:
        rows = await conn.fetch(
            """
            SELECT h.fg_sku_name
              FROM bom_header h
             WHERE COALESCE(NULLIF(h.process_category, ''), NULL) IS NOT NULL
               AND EXISTS (SELECT 1 FROM bom_process_route r WHERE r.bom_id = h.bom_id)
            """
        )
    return {r["fg_sku_name"] for r in rows}


# ── GAP LIST (grouped by family) ─────────────────────────────────────────────

async def get_routing_gaps(conn, *, entity: str | None = None,
                           family: str | None = None,
                           csv_path: Path | None = None) -> dict:
    """The grouped, still-outstanding routing-gap list.

    Reads the offline gap union, EXCLUDES articles already routed/promoted
    (process_category + >=1 route step — so the ~108 Slice-7 ones drop off),
    groups the rest by classify_family with the template suggestion. Optional
    ``entity`` / ``family`` filters narrow the result.

    Shape (the FE depends on families[].articles[]):
      {
        "total": int,
        "families": [
          { "family": str, "suggested_process_category": str, "count": int,
            "needs_review": bool,
            "articles": [
              { "article": str, "in_all_sku": bool,
                "current_process_category": str|null,
                "suggested_process_category": str } ] } ] }
    """
    union = load_gap_union(csv_path)
    routed = await _already_routed_names(conn)

    # Existing process_category per name (so a BOM_PARENT_NO_ROUTING header that
    # already carries a category — but no route — surfaces its current value).
    pc_rows = await conn.fetch(
        "SELECT fg_sku_name, process_category FROM bom_header WHERE customer_name IS NULL"
    )
    current_pc: dict[str, str | None] = {}
    for r in pc_rows:
        # keep first non-empty seen
        if r["fg_sku_name"] not in current_pc or not current_pc[r["fg_sku_name"]]:
            current_pc[r["fg_sku_name"]] = r["process_category"] or None

    ent_filter = (entity or "").strip().lower() or None

    grouped: dict[str, dict] = {}
    for r in union:
        name = r["article"]
        if name in routed:
            continue  # already left the gap
        row_entity = _csv_entity(r.get("catalog_entity"))
        if ent_filter and row_entity != ent_filter:
            continue
        fam = classify_family(name)
        if family and fam != family:
            continue
        bucket = grouped.get(fam)
        if bucket is None:
            bucket = grouped[fam] = {
                "family": fam,
                "suggested_process_category": FAMILY_TEMPLATES.get(fam, ""),
                "needs_review": family_needs_review(fam),
                "articles": [],
            }
        bucket["articles"].append({
            "article": name,
            "in_all_sku": _truthy(r.get("in_all_sku")),
            "current_process_category": current_pc.get(name),
            "suggested_process_category": FAMILY_TEMPLATES.get(fam, ""),
        })

    families = []
    for fam in sorted(grouped):
        b = grouped[fam]
        b["articles"].sort(key=lambda a: a["article"].lower())
        b["count"] = len(b["articles"])
        families.append(b)

    total = sum(b["count"] for b in families)
    return {"total": total, "families": families}


# ── APPLY (promote assignments) ──────────────────────────────────────────────

async def promote_articles(conn, assignments: list[dict], *,
                           performed_by: str | None = None,
                           csv_path: Path | None = None) -> dict:
    """Apply confirmed routing assignments — upsert bom_header + derive routes.

    REUSES the Slice-7 promote logic verbatim: it calls the SAME
    scripts.promote_fg_master_gaps.promote_one primitive (now parameterised on
    process_category), so the online apply and the one-off script derive identical
    headers + routes (the split-on-'+' route derivation lives in ONE place).

    Handles BOTH gaps: net-new (CATALOG_FG_NOT_IN_FG_MASTER -> INSERT) and
    existing-header-no-routing (BOM_PARENT_NO_ROUTING -> route in place).

    Each assignment: {"article": str, "process_category": str}. A blank/empty
    process_category is skipped (status "skipped_no_pc") — production must supply a
    real category for the transform/Other families. Idempotent (promote_one's
    upsert + ON CONFLICT DO NOTHING routes). One bad assignment does NOT abort the
    rest: each runs inside its own SAVEPOINT (conn.transaction()), and errors are
    collected. MUST be called inside an outer transaction.

    Returns:
      { "applied": int, "skipped": int,
        "results": [ {"article": str,
                      "status": "promoted|routed_existing|skipped_no_pc|error",
                      "bom_id": int|null, "detail": str} ] }
    """
    # Lazy import of the SHARED Slice-7 primitive. scripts/ is a top-level
    # namespace package; ensure the repo root is importable regardless of CWD
    # (the script itself does the same sys.path dance at its top).
    import sys
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from scripts.promote_fg_master_gaps import promote_one

    # CSV entity lookup so a net-new header gets the right entity (cfpl/cdpl), the
    # same value the Slice-7 script would assign. Built once.
    entity_by_name: dict[str, str | None] = {}
    try:
        for r in load_gap_union(csv_path):
            entity_by_name.setdefault(r["article"], _csv_entity(r.get("catalog_entity")))
    except FileNotFoundError:
        pass  # CSV optional for apply — entity falls back to None (generic header)

    applied = 0
    skipped = 0
    results: list[dict] = []

    for a in assignments:
        article = (a.get("article") or "").strip()
        pc = (a.get("process_category") or "").strip()
        if not article:
            skipped += 1
            results.append({"article": a.get("article"), "status": "error",
                            "bom_id": None, "detail": "missing article name"})
            continue
        if not pc:
            skipped += 1
            results.append({"article": article, "status": "skipped_no_pc",
                            "bom_id": None,
                            "detail": "blank process_category — needs per-item review"})
            continue

        entity = entity_by_name.get(article)
        try:
            # Per-article SAVEPOINT: a failure rolls back ONLY this article so the
            # rest of the batch still commits (collect-errors, don't abort).
            async with conn.transaction():
                out = await promote_one(conn, article, entity, pc)
            if out["status"] == "no_header":
                # Should not happen for a name that surfaced in the gap list, but
                # an existing-recipe gap with no generic header can hit it.
                skipped += 1
                results.append({"article": article, "status": "error",
                                "bom_id": None,
                                "detail": "no generic bom_header to route"})
                continue
            applied += 1
            status = "promoted" if out["status"] == "created" else "routed_existing"
            results.append({
                "article": article, "status": status, "bom_id": out["bom_id"],
                "detail": f"process_category={pc!r}, routes_added={out.get('routes_added', 0)}",
            })
        except Exception as exc:  # noqa: BLE001 — one bad row mustn't kill the batch
            skipped += 1
            results.append({"article": article, "status": "error", "bom_id": None,
                            "detail": f"{type(exc).__name__}: {exc}"})

    return {"applied": applied, "skipped": skipped, "results": results}


# ── WORKSHEET (CSV for production to fill) ────────────────────────────────────

WORKSHEET_COLUMNS = [
    "article", "family", "in_all_sku",
    "suggested_process_category", "assigned_process_category",
]


def _csv_safe(value) -> str:
    """Neutralise spreadsheet formula injection. A cell whose text begins with
    = + - @ (or a leading tab/CR) is interpreted as a live formula by Excel /
    Sheets on open; article names come from a master file, but defensively prefix
    such cells with a single quote so they render as literal text."""
    s = "" if value is None else str(value)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


async def build_worksheet_csv(conn, *, entity: str | None = None,
                              family: str | None = None,
                              csv_path: Path | None = None) -> str:
    """Render the outstanding gap list as a CSV string for production to fill.

    Columns: article, family, in_all_sku, suggested_process_category,
    assigned_process_category (blank — production fills it, then POSTs apply).
    Same exclusion/filtering as get_routing_gaps.
    """
    gaps = await get_routing_gaps(conn, entity=entity, family=family, csv_path=csv_path)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(WORKSHEET_COLUMNS)
    for fam in gaps["families"]:
        for art in fam["articles"]:
            writer.writerow([
                _csv_safe(art["article"]),
                _csv_safe(fam["family"]),
                "Y" if art["in_all_sku"] else "N",
                _csv_safe(art["suggested_process_category"]),
                "",  # assigned_process_category — production fills in
            ])
    return buf.getvalue()
