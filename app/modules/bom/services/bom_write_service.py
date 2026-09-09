"""BOM creation backing POST /api/v1/bom.

Separate from bom_aggregate_service on purpose: that module is read-only and its
docstring is the reference for how the three tables relate. This one is the only
place in the BOM module that writes.

WHAT THIS WRITES, AND WHAT IT REFUSES TO
    One `bom_header`, its `bom_line` rows, and optionally its
    `bom_process_route` steps — in one transaction, so a BOM can never come into
    existence with half its lines.

    It is a STRICT create. If an ACTIVE header already exists for the SKU it
    returns `bom_exists` (409) rather than superseding. That differs from
    production/services/plan_v2.create_bom, which deactivates the incumbent and
    inserts at version max+1. Superseding is the more dangerous default here:
    `bom_id` is FK'd from sixteen tables and `bom_line_id` from seven, with live
    job cards pointing at them at any time, so a double-submitted form must not
    be able to deactivate the BOM the floor is currently running.

THE TALLY REFRESH CAN DELETE LINES CREATED HERE
    `scripts/refresh_bom_from_tally.py` reconciles each BOM it matches in the
    Tally export against the database, and for a DB line the sheet does not
    have:

        item_type == 'sfg'          -> kept  (Tally cannot see SFG lines)
        bom_line_id in referenced   -> kept  (a job card already points at it)
        otherwise                   -> DELETED

    So a hand-created rm/pm line, on a BOM whose FG name appears in the sheet,
    that no job card references yet, is removed on the next refresh. Nothing in
    this module can prevent that — the sheet is the system of record for
    FG->component composition — so the create RESPONSE carries a warning
    instead -- see `_tally_warning`.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import asyncpg

from app.core.helpers import normalise_key

# NUMERIC(15,3) on bom_line.quantity_per_unit. Anything smaller rounds to 0.000
# on insert, and a zero-quantity line is worse than an imprecise one: it stays
# on the BOM so nobody notices it is missing, but the planner indents nothing
# and the material is never issued. refresh_bom_from_tally.q3 clamps to this
# for the same reason.
MIN_QTY_PER_UNIT = 0.001

# NUMERIC(5,4) on bom_header.allowed_balance_tolerance_pct, so >= 10 overflows
# the column outright. It is also read as a FRACTION, not a percent, despite the
# name: jc_accounting_v2 does `abs(diff) / total_input <= tolerance_pct`. A
# value of 2 (meaning "2%") therefore passes any imbalance up to 200% and
# silently disables the R9 unbalanced-close gate for that BOM forever.
MAX_TOLERANCE = 1.0

# bom_line.item_type. 'sfg' is deliberately allowed: the column already holds it
# in production (050_sfg_foundation added consumed_at_stage for exactly these),
# and it is the ONLY line type the Tally refresh never deletes, which makes it
# the safest thing to hand-create rather than something to forbid.
ITEM_TYPES = ("rm", "pm", "sfg")

# bom_line_staging_method_check. Mirrored here so a bad value is a 400 naming the
# field rather than a 500 from the database rejecting the INSERT.
STAGING_METHODS = ("pick", "backflush", "floor_stock")

# bom_header_entity_check.
ENTITIES = ("cfpl", "cdpl")

# Header columns this endpoint may set, in INSERT order. A whitelist, not
# **payload: bom_id and version are server-assigned, is_active is always TRUE on
# create, and created_at defaults. Anything not listed here is ignored rather
# than silently forwarded to the database.
_HEADER_COLS = (
    "fg_sku_name", "customer_name", "pack_size_kg", "entity",
    "item_group", "sub_group", "process_category", "business_unit", "factory",
    "floors", "machines", "shelf_life_days", "gst_rate", "hsn_sac",
    "inventory_group", "customer_code", "output_uom", "bar_line_process",
    "effective_from", "effective_to", "notes",
)

_LINE_COLS = (
    "material_sku_name", "item_type", "quantity_per_unit", "uom", "loss_pct",
    "godown", "can_use_offgrade", "offgrade_max_pct", "unit_rate_inr",
    "process_stage", "staging_method", "consumed_at_stage",
)

_ROUTE_COLS = (
    "process_name", "stage", "std_time_min", "loss_pct", "qc_check",
    "machine_type", "practical_operation", "stage_bucket",
    "input_kind", "output_kind", "input_code", "output_code",
)


def _err(code: str, message: str, **details) -> dict:
    return {"error": code, "message": message, "details": details or None}


def _norm_name(v: Any) -> str:
    """Canonicalise a BOM name. Thin wrapper over the shared helper.

    Stored FG and material names carry U+00A0, zero-width characters and doubled
    spaces, so a name typed into a form and the "same" name in the database
    routinely differ by invisible characters. app.core.helpers.normalise_key
    already handles all of that (plus cp1252 mojibake and NFC) and is what
    master_ingest matches on -- reimplementing a weaker version here would make
    this endpoint disagree with the ingest about what "the same name" means.
    """
    return normalise_key(v)


# The SAME fold as normalise_key, expressed in SQL so BOTH sides of the
# comparison are canonical.
#
# THIS IS THE POINT: normalising only the bind parameter is worse than not
# normalising at all, because it looks like a safeguard while missing exactly
# the rows it exists for. An incumbent stored with a U+00A0 is invisible to
# `fg_sku_name ILIKE 'ROASTED CASHEW 100G'` -- U+00A0 is not U+0020 -- so no 409
# fires; and uq_bom_header_active_fg (032_b11_polish.sql:39, a UNIQUE on the
# LITERAL column WHERE is_active) does not fire either, because the two strings
# genuinely differ. The result is TWO active headers for one SKU: every
# downstream `fg_sku_name ILIKE $1 AND is_active` resolver then picks whichever
# the caller's spelling matches, and refresh_bom_from_tally finds two candidates
# for that norm key and skips the FG from every future refresh.
#
# `=` and NOT ILIKE, deliberately: ILIKE treats the bind value as a PATTERN, so
# an FG name containing `_` or `%` matches unrelated SKUs -- 'GRANOLA BAR_40G'
# would match an existing 'GRANOLA BAR 40G' and refuse a legitimate create with
# a 409 naming the wrong product. lower() on both sides keeps the
# case-insensitivity ILIKE was there for.
#
# This cannot use uq_bom_header_active_fg (an expression, not the bare column).
# bom_header is a few thousand rows and this runs once per create, so a seq scan
# beats maintaining an expression index.
_NORM_FG_SQL = (
    "btrim(regexp_replace(replace(lower(fg_sku_name), chr(160), ' '),"
    r" '\s+', ' ', 'g'))"
)


def _validate(payload: dict) -> dict | None:
    """Return an error envelope, or None when the payload is sound."""
    if not _norm_name(payload.get("fg_sku_name")):
        return _err("no_sku", "fg_sku_name is required")

    entity = payload.get("entity")
    if entity is not None and entity not in ENTITIES:
        return _err("bad_entity",
                    f"entity must be one of {', '.join(ENTITIES)}",
                    entity=entity)

    lines = payload.get("lines") or []
    if not lines:
        return _err("no_lines", "At least one BOM line is required")

    for i, ln in enumerate(lines, start=1):
        if not _norm_name(ln.get("material_sku_name")):
            return _err("no_material", f"Line {i}: material_sku_name is required",
                        line_number=i)
        if ln.get("item_type") not in ITEM_TYPES:
            return _err("bad_item_type",
                        f"Line {i}: item_type must be one of {', '.join(ITEM_TYPES)}",
                        line_number=i, item_type=ln.get("item_type"))
        try:
            qty = float(ln.get("quantity_per_unit"))
        except (TypeError, ValueError):
            qty = 0.0
        if not qty > 0:
            return _err("bad_qty",
                        f"Line {i}: quantity_per_unit must be a number > 0",
                        line_number=i, quantity_per_unit=ln.get("quantity_per_unit"))
        # > 0 is not enough: the column is NUMERIC(15,3), so 0.0004 is stored as
        # 0.000. The line then sits on the BOM looking present while the planner
        # indents nothing for it -- the failure refresh_bom_from_tally.q3 clamps
        # against. Refuse rather than silently clamp: only the author knows
        # whether the intended unit was kg or g.
        if round(qty, 3) < MIN_QTY_PER_UNIT:
            return _err("qty_below_precision",
                        f"Line {i}: quantity_per_unit {qty} rounds to 0.000 at "
                        f"the column's NUMERIC(15,3) precision. Use at least "
                        f"{MIN_QTY_PER_UNIT}, or express the quantity in a "
                        f"smaller unit.",
                        line_number=i, quantity_per_unit=qty,
                        minimum=MIN_QTY_PER_UNIT)
        sm = ln.get("staging_method")
        if sm is not None and sm not in STAGING_METHODS:
            return _err("bad_staging_method",
                        f"Line {i}: staging_method must be one of "
                        f"{', '.join(STAGING_METHODS)}",
                        line_number=i, staging_method=sm)

    tol = payload.get("allowed_balance_tolerance_pct")
    if tol is not None:
        try:
            tol_f = float(tol)
        except (TypeError, ValueError):
            tol_f = -1.0
        # Upper bound is 1.0, not the column's 9.9999: the value is used as a
        # FRACTION (abs(diff)/total_input <= tolerance_pct), so anything >= 1
        # accepts a 100%+ imbalance and turns the R9 unbalanced-close gate off.
        # The name says "pct", which is exactly why someone will type 2.
        if not 0 <= tol_f <= MAX_TOLERANCE:
            return _err("bad_tolerance",
                        f"allowed_balance_tolerance_pct must be a FRACTION "
                        f"between 0 and {MAX_TOLERANCE} (0.001 = 0.1%), not a "
                        f"percentage. It is compared as "
                        f"abs(diff)/total_input <= this value, so {tol} would "
                        f"disable the unbalanced-close gate.",
                        allowed_balance_tolerance_pct=tol)

    for i, st in enumerate(payload.get("route") or [], start=1):
        if not _norm_name(st.get("process_name")):
            return _err("no_process_name", f"Route step {i}: process_name is required",
                        step_number=i)
    return None


def _tally_warning(lines: list[dict]) -> str | None:
    """Warn whenever the BOM carries rm/pm lines. Unconditional, on purpose.

    An earlier version gated this on "does this FG already have BOM history",
    reasoning that a brand-new FG is absent from the Tally sheet. That is
    BACKWARDS. refresh_bom_from_tally loads its candidates with
    `WHERE is_active` and matches each sheet FG by name (then by normalised
    key); it never consults version history. A BOM created here is is_active =
    TRUE, so it is a candidate on the very next run regardless of whether the FG
    had earlier headers -- which means the highest-risk case (an FG Tally
    already exports, getting its first ERP BOM through this endpoint, with
    hand-added lines the sheet lacks) was precisely the case that got NO
    warning, while an FG with only deactivated history got the warning as noise.

    The honest signal is whether the FG name appears in the Tally sheet, and
    this process cannot see the sheet. So: warn whenever there is something to
    lose. sfg lines are excluded from the count because the refresh explicitly
    keeps them.
    """
    at_risk = [ln for ln in lines if ln.get("item_type") != "sfg"]
    if not at_risk:
        return None
    return (
        f"{len(at_risk)} of {len(lines)} lines are rm/pm. If this FG appears in "
        f"the Tally BOM export, the next refresh "
        f"(scripts/refresh_bom_from_tally.py) will DELETE any rm/pm line the "
        f"sheet does not contain, unless a job card already references it. Add "
        f"these materials to the Tally BOM sheet to make them permanent."
    )


async def create_bom(conn, payload: dict, *, created_by: str | None = None) -> dict:
    """Create one BOM. MUST run inside an outer transaction.

    Returns {bom_id, fg_sku_name, version, entity, lines_created,
    route_steps_created, warnings[]} or an {error, message, details} envelope
    the router maps to 400/409.
    """
    invalid = _validate(payload)
    if invalid:
        return invalid

    fg_sku_name = _norm_name(payload.get("fg_sku_name"))

    # Case-insensitive, because every consumer RESOLVES a BOM case-insensitively
    # (plan_v2.create_plan and the detail path both use
    # `fg_sku_name ILIKE $1 AND is_active = TRUE`), so a bare `=` here would let
    # a case variant through and leave those lookups two rows to choose between.
    # The fold is on BOTH sides and the match is exact — see _NORM_FG_SQL for
    # why that matters more than it looks.
    existing = await conn.fetchrow(
        "SELECT bom_id, version, entity FROM bom_header"
        f" WHERE {_NORM_FG_SQL} = lower($1) AND is_active = TRUE"
        " ORDER BY version DESC LIMIT 1",
        fg_sku_name,
    )
    if existing:
        return _err(
            "bom_exists",
            f"An active BOM already exists for {fg_sku_name!r} "
            f"(bom_id {existing['bom_id']}, version {existing['version']}). "
            f"Deactivate it or create a new version explicitly.",
            bom_id=existing["bom_id"], version=existing["version"],
            entity=existing["entity"],
        )

    # Deliberately NOT filtered by is_active: version numbers must not be reused
    # across a SKU's lineage even after every prior header was deactivated, or a
    # (fg_sku_name, version) pair stops identifying one recipe. Doubles as the
    # "has Tally seen this FG" signal for the warning below.
    prior_max = await conn.fetchval(
        f"SELECT MAX(version) FROM bom_header WHERE {_NORM_FG_SQL} = lower($1)",
        fg_sku_name,
    )
    version = (prior_max or 0) + 1

    header = {c: payload.get(c) for c in _HEADER_COLS}
    header["fg_sku_name"] = fg_sku_name
    header["customer_name"] = _norm_name(header["customer_name"]) or None
    if header["effective_from"] is None:
        # bom_header.effective_from has NO database default, and _HEADER_COLS
        # always names the column, so omitting this would bind an explicit NULL
        # and store a BOM with no validity start. plan_v2.create_bom writes
        # CURRENT_DATE here; matching it keeps BOMs from the two paths
        # comparable, and amendments_v2 carries the value forward into every
        # future version, so a NULL would propagate.
        header["effective_from"] = date.today()
    tol = payload.get("allowed_balance_tolerance_pct")

    cols = list(_HEADER_COLS) + ["version", "is_active"]
    vals = [header[c] for c in _HEADER_COLS] + [version, True]
    if tol is not None:
        # NOT NULL DEFAULT 0.001 in the schema, so it is only named when the
        # caller actually supplied one — passing None would violate the column.
        cols.append("allowed_balance_tolerance_pct")
        vals.append(tol)

    placeholders = ", ".join(f"${i}" for i in range(1, len(vals) + 1))
    try:
        bom_id = await conn.fetchval(
            f"INSERT INTO bom_header ({', '.join(cols)})"
            f" VALUES ({placeholders}) RETURNING bom_id",
            *vals,
        )
    except asyncpg.UniqueViolationError:
        # The probe above is a check-then-insert with no lock, so two concurrent
        # creates for one SKU can both see "no incumbent". uq_bom_header_active_fg
        # (UNIQUE on fg_sku_name WHERE is_active) is what actually prevents the
        # duplicate -- but without this catch the loser gets an unhandled 500
        # instead of the same 409 a sequential double-submit gets. Same outcome,
        # same status code, whichever way the race lands.
        return _err(
            "bom_exists",
            f"An active BOM for {fg_sku_name!r} was created concurrently. "
            f"Reload and edit that one instead.",
            fg_sku_name=fg_sku_name,
        )

    # line_number is assigned 1..N from array order, never taken from the
    # client: it is UNIQUE(bom_id, line_number), so a client-supplied value
    # turns a duplicated or skipped number into a constraint violation the
    # operator cannot act on.
    lines = payload.get("lines") or []
    for i, ln in enumerate(lines, start=1):
        row = {c: ln.get(c) for c in _LINE_COLS}
        row["material_sku_name"] = _norm_name(row["material_sku_name"])
        row["loss_pct"] = row["loss_pct"] or 0
        row["offgrade_max_pct"] = row["offgrade_max_pct"] or 0
        row["can_use_offgrade"] = bool(row["can_use_offgrade"])
        await conn.execute(
            f"INSERT INTO bom_line (bom_id, line_number, {', '.join(_LINE_COLS)})"
            f" VALUES ($1, $2, "
            f"{', '.join(f'${i}' for i in range(3, 3 + len(_LINE_COLS)))})",
            bom_id, i, *[row[c] for c in _LINE_COLS],
        )

    route = payload.get("route") or []
    for i, st in enumerate(route, start=1):
        row = {c: st.get(c) for c in _ROUTE_COLS}
        row["process_name"] = _norm_name(row["process_name"])
        # `stage` is the slug downstream code orders and matches on
        # (bar_line_service._stage_for, order_steps_packing_last). Defaulting it
        # to a slug of the process name beats storing NULL, which would make the
        # step invisible to every one of those consumers.
        row["stage"] = row["stage"] or _slug(row["process_name"])
        row["loss_pct"] = row["loss_pct"] or 0
        await conn.execute(
            f"INSERT INTO bom_process_route (bom_id, step_number, {', '.join(_ROUTE_COLS)})"
            f" VALUES ($1, $2, "
            f"{', '.join(f'${i}' for i in range(3, 3 + len(_ROUTE_COLS)))})",
            bom_id, i, *[row[c] for c in _ROUTE_COLS],
        )

    warnings = []
    tally = _tally_warning(lines)
    if tally:
        warnings.append(tally)

    # Duplicated materials are ALLOWED (consumed_at_stage exists precisely so one
    # material can appear at two stages) but they are exactly what the Tally
    # refresh culls first — it keeps `group[0]` and marks `group[1:]` for
    # deletion regardless of the sheet — so say so rather than let it look
    # accidental later.
    seen: dict[str, int] = {}
    for ln in lines:
        k = _norm_name(ln.get("material_sku_name")).lower()
        seen[k] = seen.get(k, 0) + 1
    dupes = sorted(k for k, n in seen.items() if n > 1)
    if dupes:
        warnings.append(
            f"{len(dupes)} material(s) appear on more than one line "
            f"({', '.join(dupes[:5])}). This is permitted, but the Tally refresh "
            f"keeps only the first line per material and marks the rest for "
            f"deletion unless they are sfg or already referenced by a job card."
        )

    return {
        "bom_id": bom_id,
        "fg_sku_name": fg_sku_name,
        "version": version,
        "entity": header["entity"],
        "lines_created": len(lines),
        "route_steps_created": len(route),
        "created_by": created_by,
        "warnings": warnings,
    }


def _slug(v: str) -> str:
    """'Metal Detection' -> 'metal_detection'. Matches the shape of the slugs
    bar_line_service writes into bom_process_route.stage."""
    return "_".join("".join(c if c.isalnum() else " " for c in v).split()).lower()
