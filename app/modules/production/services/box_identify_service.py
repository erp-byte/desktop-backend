"""Universal box identify: scan a QR, tell which table the box lives in.

Routing by QR structure (per the label formats in this ERP):
  * JSON  {"tx":"TR-...","bi":"91483060-1"}  -> match (box_id, transaction_no)
  * bare  "91483060-1"                       -> sfg_box by carton_id, then box id alone.

WHY THIS IS ONE QUERY, NOT A LOOP OF POINT LOOKUPS
--------------------------------------------------
`box_id` is in no unique key anywhere in this schema. Every module mints it from
the last 8 digits of epoch-ms, so the base repeats about every 27.7 hours, and
`delete_lot127024_2boxes.py` states it plainly: "box_id alone is NOT unique".
A sequential per-table `LIMIT 1` therefore returns whichever table happened to be
probed first — a *different physical box* than the one scanned, reported as
`found: True`. So we UNION ALL the candidate branches, ORDER BY an explicit
priority, and take LIMIT 2 so a genuine multi-match is detected and logged rather
than silently resolved. One round trip instead of ten.

WHY COLUMNS ARE VALIDATED BEFORE THE QUERY IS BUILT
---------------------------------------------------
A UNION couples every branch: one missing column fails the WHOLE statement at
PREPARE. That has already happened here — selecting `batch_number` (absent on
both tenants) made every box lookup 500. And the previous defence,
`except asyncpg.PostgresError: return None`, was worse than the disease: a
mistyped column became a permanent silent NOT FOUND in production.

So `_plan()` builds only the branches whose columns actually exist, from one
cached `information_schema` read per table. A table that is missing, or has
drifted, is dropped from the plan and logged at WARNING — never guessed at, and
never able to poison the other branches.

NOT COVERED, DELIBERATELY
-------------------------
  * {cfpl|cdpl}_boxes (pre-v2) — of its columns only `lot_number` has any direct
    evidence; the rest are inferred from its _v2 sibling. Adding it on that basis
    would reintroduce exactly the silent-miss this module now prevents. It needs a
    live `information_schema` check first. (It would then be *excluded by the
    planner* rather than silently failing, but an unverified branch that never
    matches is still a lie by omission, so it stays out until confirmed.)
  * pending_transfer_stock — holds every In-Transit box. Today an in-transit box
    resolves to its source table or to nothing, and never reports that it is
    already committed to a challan. Adding it is a product decision, not a bug fix.

PREFIX ROUTING IS NOT USED, ON PURPOSE
--------------------------------------
`TR-` is minted by seven independent generators in three formats and lands in
nine-plus table families — a cold-destined inward writes the same `TR-` string
into both `{p}_boxes_v2` and `{p}_cold_stocks` for the same physical box. So a
transaction prefix cannot select a table. `TRANS`/`JB` label the transfer and
job-work *challans* (header tables); the box rows under them carry the source
`TR-` inward number. Prefix is used only to ORDER branches (see _PRIO_HINTS),
never to include or exclude one.
"""
from __future__ import annotations

import json
import logging

import asyncpg

logger = logging.getLogger(__name__)

_ENTITIES = ("cfpl", "cdpl")


# ── branch specs ─────────────────────────────────────────────────────────────
# Each entry projects its own columns onto ONE common shape so the branches can
# be UNIONed. Column names genuinely differ per table — `article` vs
# `article_description` vs `item_description`, `lot_no` vs `lot_number`,
# `weight_kg` vs `net_weight` — so the mapping is per branch, not per alias.
#
#   need    : columns that must exist for the branch to be built at all
#   entity  : 'prefix' (read from the cfpl_/cdpl_ table name), a column, or None
#             when the table carries no reliable signal (guessing is worse).
#
# `count` is projected ONLY where it is genuinely per-box. On bulk_entry_boxes and
# cold_stocks it is a pile/article total replicated onto every row, so one carton
# would read as e.g. 1403.
_SPECS = [
    {
        "key": "boxes_v2", "split": True, "table": "{p}_boxes_v2",
        "need": ("box_id", "transaction_no"),
        "box": "box_id", "txn": "transaction_no",
        "desc": "article_description", "lot": "lot_number",
        "net": "net_weight", "gross": "gross_weight",
        # No `status` column on this table (interunit_tools.py: "current target
        # — no status col"). Status lives on {p}_transactions_v2.
        "status": None, "count": None, "entity": "prefix",
    },
    {
        "key": "bulk_entry_boxes", "split": True, "table": "{p}_bulk_entry_boxes",
        "need": ("box_id", "transaction_no"),
        "box": "box_id", "txn": "transaction_no",
        "desc": "article_description", "lot": "lot_number",
        "net": "net_weight", "gross": "gross_weight",
        "status": "status", "count": None, "entity": "prefix",
    },
    {
        "key": "cold_stocks", "split": True, "table": "{p}_cold_stocks",
        "need": ("box_id", "transaction_no"),
        "box": "box_id", "txn": "transaction_no",
        "desc": "item_description", "lot": "lot_no",
        "net": "weight_kg", "gross": None,
        "status": None, "count": None, "entity": "prefix",
    },
    {
        # The return id lives on the HEADER (rtv_id, "CR-…"); the box row has no
        # transaction_no at all. This is the one branch that must join.
        "key": "rtv_boxes", "split": True, "table": "{p}_rtv_boxes",
        "join": "{p}_rtv_header", "join_on": "b.header_id = h.id",
        "need": ("box_id", "header_id"),
        "join_need": ("id", "rtv_id"),
        "box": "box_id", "txn": "h.rtv_id",
        "desc": "article_description", "lot": "lot_number",
        "net": "net_weight", "gross": "gross_weight",
        "status": None, "count": None, "entity": "prefix",
    },
    {
        "key": "interunit_transfer_boxes", "split": False, "table": "interunit_transfer_boxes",
        "need": ("box_id", "transaction_no"),
        "box": "box_id", "txn": "transaction_no",
        "desc": "article", "lot": "lot_number",
        "net": "net_weight", "gross": "gross_weight",
        # Entity comes from the header's sites, not the table name. Not worth a
        # join for a field the caller treats as a hint.
        "status": None, "count": None, "entity": None,
    },
    {
        # Three box identities: a relabelled box keeps its pre-relabel id in
        # original_box_id, and inward_box_id traces to the box it was received as.
        # Matching box_id alone silently misses every relabelled box.
        "key": "interunit_transfer_in_boxes", "split": False, "table": "interunit_transfer_in_boxes",
        "need": ("box_id", "transaction_no"),
        "alt_box": ("original_box_id", "inward_box_id"),
        "box": "box_id", "txn": "transaction_no",
        "desc": "article", "lot": "lot_number",
        "net": "net_weight", "gross": "gross_weight",
        "status": None, "count": None, "entity": None,
    },
    {
        "key": "cold_transfer_inboxes", "split": False, "table": "cold_transfer_inboxes",
        "need": ("box_id", "transaction_no"),
        "box": "box_id", "txn": "transaction_no",
        "desc": "item_description", "lot": "lot_no",
        "net": "weight_kg", "gross": None,
        "status": None, "count": None, "entity": None,
    },
    {
        "key": "jb_inward_boxes", "split": False, "table": "jb_inward_boxes",
        "need": ("box_id", "transaction_no"),
        "box": "box_id", "txn": "transaction_no",
        "desc": "item_description", "lot": "lot_no",
        "net": "net_weight", "gross": "gross_weight",
        "status": None, "count": None, "entity": None,
    },
    {
        "key": "po_box", "split": False, "table": "po_box",
        "need": ("box_id", "transaction_no"),
        "box": "box_id", "txn": "transaction_no",
        # Description is not on po_box; it lives on the PO line.
        "desc": None, "lot": "lot_number",
        "net": "net_weight", "gross": "gross_weight",
        "status": None, "count": None, "entity": None,
    },
    {
        # Keyed by carton_id and carries NO transaction number, so it can only ever
        # join the box-id-only pass.
        "key": "sfg_box", "split": False, "table": "sfg_box", "no_txn": True,
        "need": ("carton_id",),
        "box": "carton_id", "txn": None,
        "desc": "fg_sku_name", "desc_fallback": "sfg_code", "lot": None,
        "net": "net_weight", "gross": "gross_weight",
        "status": "status", "count": None, "entity": "entity",
        "job_card": "job_card_number",
    },
]

# Ordering hints ONLY. A prefix can never include or exclude a branch (see the
# module docstring) — it just floats the likeliest table to the top so that when
# two tables legitimately hold the same (box_id, transaction_no), the scan
# returns the more specific one. CR- and RTV- are ONE family: the rename was
# forward-only and both are live on printed labels.
_PRIO_HINTS = (
    (("BE-",), "bulk_entry_boxes"),
    (("CR-", "RTV-"), "rtv_boxes"),
    (("PLAN-", "MPG-"), "sfg_box"),
)


def _hinted_first(txn: str | None) -> str | None:
    if not txn:
        return None
    up = txn.strip().upper()
    for prefixes, key in _PRIO_HINTS:
        if any(up.startswith(p) for p in prefixes):
            return key
    return None


# ── column discovery ─────────────────────────────────────────────────────────
# Process-lifetime cache. These tables don't gain or lose columns at runtime, and
# the read is one catalog query per table.
# ponytail: restart the process to pick up a schema change (rare; acceptable).
_COLUMNS: dict[str, frozenset[str]] = {}


async def _columns(conn, table: str) -> frozenset[str]:
    """Column names for `table`, or an empty set if it does not exist.

    An empty set makes a missing table and a drifted table take the same path —
    the branch is dropped from the plan — which is what we want: neither may
    break the other branches, and both get logged.
    """
    hit = _COLUMNS.get(table)
    if hit is None:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = $1",
            table,
        )
        hit = frozenset(r["column_name"] for r in rows)
        _COLUMNS[table] = hit
    return hit


def _tables_for(spec: dict) -> list[tuple[str, str | None, str | None]]:
    """(box_table, join_table, entity_literal) for a spec — twice if entity-split."""
    if not spec.get("split"):
        return [(spec["table"], spec.get("join"), None)]
    out = []
    for p in _ENTITIES:
        out.append((
            spec["table"].format(p=p),
            spec["join"].format(p=p) if spec.get("join") else None,
            p,
        ))
    return out


# ── planning (pure — no DB, so it is directly unit-testable) ──────────────────
def _qualify(name: str | None, cols: frozenset[str], alias: str) -> str:
    """`b.col` when the column exists, a typed NULL placeholder when it does not.

    Projecting NULL rather than dropping the branch keeps every arm of the UNION
    the same width — the branch has already passed its `need` check, so what is
    missing here is optional enrichment, not identity.
    """
    if not name:
        return "NULL"
    if "." in name:                # already qualified (e.g. h.rtv_id)
        return name
    return f"{alias}.{name}" if name in cols else "NULL"


def _desc_expr(spec: dict, cols: frozenset[str], alias: str) -> str:
    """Description column, with an optional fallback.

    sfg_box stores the name in fg_sku_name but older rows carry only sfg_code —
    the previous alias tuple covered both, and dropping that returned a null
    article for those cartons, which the scan write path then rejects.
    """
    primary = _qualify(spec.get("desc"), cols, alias)
    fb = _qualify(spec.get("desc_fallback"), cols, alias)
    if fb == "NULL":
        return primary
    if primary == "NULL":
        return fb
    return f"COALESCE(NULLIF({primary}, ''), {fb})"


def _plan(available: dict[str, frozenset[str]], *, with_txn: bool,
          hint: str | None = None) -> tuple[str, list[str]]:
    """Build the UNION ALL over every branch whose columns are all present.

    `available` maps table name -> its column set. Returns (sql, sources) where
    `sources` lists the tables actually included, in priority order. Returns
    ("", []) when nothing qualifies.
    """
    ordered = sorted(
        _SPECS,
        key=lambda s: (0 if hint and s["key"] == hint else 1, _SPECS.index(s)),
    )

    branches: list[str] = []
    sources: list[str] = []
    prio = 0

    for spec in ordered:
        if with_txn and spec.get("no_txn"):
            continue  # carries no transaction number; box-id pass only
        for table, join, ent in _tables_for(spec):
            cols = available.get(table) or frozenset()
            if not cols:
                continue
            if not set(spec["need"]).issubset(cols):
                continue
            jcols = None
            if join:
                jcols = available.get(join) or frozenset()
                if not set(spec.get("join_need") or ()).issubset(jcols):
                    continue

            prio += 1
            b, j = "b", "h"

            def col(name, _cols=cols, _b=b):
                return _qualify(name, _cols, _b)

            # box-id predicate: include the alternate identity columns that
            # actually exist, so a relabelled box still resolves.
            id_cols = [spec["box"]] + [c for c in (spec.get("alt_box") or ()) if c in cols]
            id_pred = " OR ".join(f"{b}.{c}::text = $1" for c in id_cols)
            where = f"({id_pred})"
            if with_txn:
                where += f" AND {col(spec['txn'])}::text = $2"

            frm = f"{table} {b}"
            if join:
                frm += f" JOIN {join} {j} ON {spec['join_on']}"

            ent_expr = "NULL"
            if spec.get("entity") == "prefix" and ent:
                ent_expr = f"'{ent}'"
            elif spec.get("entity") and spec["entity"] != "prefix":
                ent_expr = col(spec["entity"])

            branches.append(
                f"SELECT {col(spec['box'])}::text AS box_id, "
                f"{col(spec['txn'])}::text AS transaction_no, "
                f"{_desc_expr(spec, cols, b)}::text AS item_description, "
                f"{col(spec.get('lot'))}::text AS lot_number, "
                f"{col(spec.get('net'))}::numeric AS net_weight, "
                f"{col(spec.get('gross'))}::numeric AS gross_weight, "
                f"{col(spec.get('count'))}::bigint AS count, "
                f"{col(spec.get('status'))}::text AS status, "
                f"{col(spec.get('job_card'))}::text AS job_card_number, "
                f"{ent_expr}::text AS company, "
                f"'{table}'::text AS _src, {prio}::int AS _prio "
                f"FROM {frm} WHERE {where}"
            )
            sources.append(table)

    if not branches:
        return "", []
    sql = ("SELECT * FROM (" + " UNION ALL ".join(branches) +
           ") u ORDER BY _prio, box_id LIMIT 2")
    return sql, sources


# Tables already reported as unusable. The column probe is cached, but the
# WARNING must be rate-limited separately or a scan endpoint reprints the same
# lines on every request and the signal is lost in its own noise.
_WARNED: set[str] = set()


async def _available(conn, *, with_txn: bool) -> dict[str, frozenset[str]]:
    """Column sets for every table any branch might use, logging what is absent."""
    out: dict[str, frozenset[str]] = {}
    for spec in _SPECS:
        if with_txn and spec.get("no_txn"):
            continue
        for table, join, _ in _tables_for(spec):
            for t in (table, join):
                if t and t not in out:
                    out[t] = await _columns(conn, t)
            if join:
                jmissing = set(spec.get("join_need") or ()) - (out.get(join) or frozenset())
                if jmissing and join not in _WARNED:
                    _WARNED.add(join)
                    logger.warning(
                        "box-identify: join table %r is absent or missing %s — branch "
                        "%r skipped. rtv_boxes is the ONLY source of a CR-/RTV- "
                        "transaction number, so customer-return boxes will scan as "
                        "NOT FOUND until this is corrected.",
                        join, sorted(jmissing), spec["key"])
            cols = out.get(table) or frozenset()
            if not cols:
                if table not in _WARNED:
                    _WARNED.add(table)
                    logger.warning(
                        "box-identify: table %r absent — branch %r skipped. A box "
                        "that lives only there will scan as NOT FOUND.",
                        table, spec["key"])
            else:
                missing = set(spec["need"]) - cols
                if missing and table not in _WARNED:
                    _WARNED.add(table)
                    logger.warning(
                        "box-identify: table %r is missing %s — branch %r skipped. "
                        "Boxes in that table will scan as NOT FOUND until the column "
                        "map is corrected.", table, sorted(missing), spec["key"])
    return out


# ── result shaping ───────────────────────────────────────────────────────────
def _num(v):
    if v is None:
        return None
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return v


def _result(row: dict, matched_by: str, *, ambiguous_with=None) -> dict:
    out = {
        "found": True,
        "table": row.get("_src"),
        "company": row.get("company"),
        "matched_by": matched_by,
        "box": {
            "box_id":           row.get("box_id"),
            "transaction_no":   row.get("transaction_no"),
            "item_description": row.get("item_description"),
            "lot_number":       row.get("lot_number"),
            "net_weight":       _num(row.get("net_weight")),
            "gross_weight":     _num(row.get("gross_weight")),
            "count":            row.get("count"),
            "status":           row.get("status"),
            "job_card_number":  row.get("job_card_number"),
        },
        # ponytail: no raw-row dump — cold_stocks carries cost columns
        # (last_purchase_rate/value) and this endpoint is scan-facing.
    }
    if ambiguous_with:
        # Surfaced, not swallowed: box_id is in no unique key, so a second hit
        # means the caller may be looking at the wrong physical box.
        out["ambiguous"] = True
        out["also_in"] = ambiguous_with
    return out


async def _run(conn, sql: str, *args) -> list[dict]:
    """Execute a planned query.

    The catch is narrow ON PURPOSE. `_plan` has already proven every table and
    column exists, so UndefinedTable/UndefinedColumn here means the schema moved
    under a cached plan — recoverable, but it must be loud. Everything else
    (DataError from a type mismatch, permissions) propagates: a scan that cannot
    answer must not pretend the box is missing.
    """
    try:
        return [dict(r) for r in await conn.fetch(sql, *args)]
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
        logger.exception(
            "box-identify: schema drifted under a cached column plan; clearing "
            "the cache so the next scan re-plans.")
        _COLUMNS.clear()
        # Clear the warning suppressor too, or the re-plan drops branches silently.
        _WARNED.clear()
        return []


async def identify_box(conn, value: str) -> dict:
    value = (value or "").strip()
    tx = bi = None
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            tx, bi = parsed.get("tx"), parsed.get("bi")
    except (ValueError, TypeError):
        pass

    if tx and bi:
        bi, tx = str(bi).strip(), str(tx).strip()
        avail = await _available(conn, with_txn=True)
        sql, _ = _plan(avail, with_txn=True, hint=_hinted_first(tx))
        rows = await _run(conn, sql, bi, tx) if sql else []
        if rows:
            extra = [r["_src"] for r in rows[1:]]
            if extra:
                logger.warning(
                    "box-identify: (box_id=%r, transaction_no=%r) matched %d tables "
                    "%s — returning %r. box_id is in no unique key, so this pair is "
                    "not guaranteed to identify one physical box.",
                    bi, tx, len(rows), [rows[0]["_src"]] + extra, rows[0]["_src"])
            return _result(rows[0], "tx+bi", ambiguous_with=extra)
        found = await _search_by_id(conn, bi, hint=_hinted_first(tx))
        return found or {"found": False, "box_id": bi}

    # bare id, or a JSON QR that only carried `bi`
    box_id = str(bi).strip() if bi else value
    if box_id:
        found = await _search_by_id(conn, box_id)
        if found:
            return found
    return {"found": False, "box_id": box_id}


async def _search_by_id(conn, box_id: str, *, hint: str | None = None) -> dict | None:
    """Box id alone, across every branch including sfg_box's carton_id.

    This pass is inherently weaker than tx+bi: without a transaction number,
    a repeated box_id base (~27.7h) can match a different physical box. A
    multi-table hit is therefore reported as ambiguous rather than resolved.
    """
    avail = await _available(conn, with_txn=False)
    sql, _ = _plan(avail, with_txn=False, hint=hint)
    if not sql:
        return None
    rows = await _run(conn, sql, box_id)
    if not rows:
        return None
    extra = [r["_src"] for r in rows[1:]]
    if extra:
        logger.warning(
            "box-identify: box_id=%r alone matched %d tables %s — returning %r. "
            "Without a transaction number this may be a different physical box.",
            box_id, len(rows), [rows[0]["_src"]] + extra, rows[0]["_src"])
    matched = "carton_id" if rows[0].get("_src") == "sfg_box" else "box_id_only"
    return _result(rows[0], matched, ambiguous_with=extra)
