"""BOM writes backing POST / PATCH / DELETE on /api/v1/bom.

Separate from bom_aggregate_service on purpose: that module is read-only and its
docstring is the reference for how the three tables relate. This one is the only
place in the BOM module that writes.

THREE OPERATIONS
    create_bom      POST   /api/v1/bom            header + lines (+ route), strict
    update_bom      PATCH  /api/v1/bom/{bom_id}   partial header edit, optional
                                                  line / route replacement
    deactivate_bom  DELETE /api/v1/bom/{bom_id}   SOFT delete: is_active = FALSE

WHAT CREATE WRITES, AND WHAT IT REFUSES TO
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

WHY DELETE IS A SOFT DELETE
    `bom_id` is referenced from plan lines, job cards, QC, costing and more, and
    `bom_line_id` from indents, consumption, balance and overrides. A hard
    DELETE would either fail on the first FK or, worse, cascade. So DELETE flips
    `is_active` to FALSE and nothing else: every resolver in the system already
    filters on `is_active = TRUE` (plan_v2.create_plan, the detail path, the
    Tally refresh), so an inactive BOM is invisible to new work while every
    existing job card keeps its recipe. It is idempotent: deactivating an
    already-inactive BOM is a 200 with `already_inactive: true`, not an error.

WHY UPDATE KEEPS bom_line_id STABLE
    PATCH with `lines` is a full replacement of the line set, but matched BY ID:
    a payload line carrying a `bom_line_id` that belongs to this BOM is UPDATEd
    in place, one without is INSERTed, and any existing line missing from the
    payload is DELETEd. Updating in place is what keeps a job card's indent /
    consumption rows pointing at the same `bom_line_id` after a quantity or
    loss% correction. A line that IS referenced cannot be deleted — the FK
    refuses — and that is surfaced as `line_referenced` (409) listing every such
    line, so the operator can keep them rather than get one opaque 500.

    `line_number` is UNIQUE(bom_id, line_number) and is reassigned 1..N from
    array order, so reordering lines swaps numbers. To make that collision-free
    the surviving lines are first parked on negative numbers (-line_number is
    unique because line_number was), then renumbered. See `_PARK_LINE_NUMBERS`.

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
    FG->component composition — so the create AND update responses carry a
    warning instead -- see `_tally_warning`.
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

# NUMERIC(5,3): bom_line.loss_pct / offgrade_max_pct, bom_process_route.loss_pct
# and bom_header.gst_rate. 99.999 is the largest value those columns hold; 100
# is a "numeric field overflow" from Postgres — a 500 that names no field —
# so it is refused here as a 400 that does.
PCT_MAX = 99.999

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

# Header columns the CREATE endpoint may set, in INSERT order. A whitelist, not
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

# Header columns the UPDATE endpoint may set. _HEADER_COLS plus the two that
# create handles specially: allowed_balance_tolerance_pct (create omits it so
# the NOT NULL DEFAULT applies; an update has no default to fall back on, so it
# is a plain column here and an explicit NULL is refused in _validate_tolerance)
# and is_active (create always writes TRUE; an update may flip it, which is how
# a deactivated BOM is brought back). STILL NOT here, on purpose: bom_id,
# version and created_at — a version number must keep identifying one recipe.
_HEADER_UPDATE_COLS = _HEADER_COLS + ("allowed_balance_tolerance_pct", "is_active")

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

# Update / deactivate read the header FOR UPDATE so two concurrent PATCHes (or a
# PATCH racing a DELETE) on one BOM serialise on the row instead of interleaving
# their line rewrites.
_LOCK_HEADER_SQL = "SELECT * FROM bom_header WHERE bom_id = $1 FOR UPDATE"

# The create probe, minus this BOM: used when an update renames the FG or
# re-activates the header, both of which can collide with another active row.
_OTHER_ACTIVE_SQL = (
    "SELECT bom_id, version, entity FROM bom_header"
    f" WHERE {_NORM_FG_SQL} = lower($1) AND is_active = TRUE AND bom_id <> $2"
    " ORDER BY version DESC LIMIT 1"
)

_LINE_IDS_SQL = "SELECT bom_line_id FROM bom_line WHERE bom_id = $1"

# Scoped to bom_id as well as bom_line_id: the id was already checked to belong
# to this BOM, but a DELETE that can only ever touch this BOM's rows is the kind
# of thing worth paying one extra predicate for.
_DELETE_LINE_SQL = "DELETE FROM bom_line WHERE bom_line_id = $1 AND bom_id = $2"

# Free the positive line_number space before renumbering. -n is unique because
# n was (UNIQUE(bom_id, line_number)), so this never collides with itself, and
# every surviving line is renumbered to a positive value straight after, so no
# negative number is ever committed.
_PARK_LINE_NUMBERS_SQL = (
    "UPDATE bom_line SET line_number = -line_number WHERE bom_id = $1"
)

_DELETE_ROUTE_SQL = "DELETE FROM bom_process_route WHERE bom_id = $1"

# The soft delete. Only is_active: effective_to, notes and every other column
# are left exactly as they were, so a deactivation is fully reversible by a
# PATCH {"is_active": true} and there is nothing to "undo" beyond the flag.
_DEACTIVATE_SQL = "UPDATE bom_header SET is_active = FALSE WHERE bom_id = $1"

_LINE_INSERT_SQL = (
    f"INSERT INTO bom_line (bom_id, line_number, {', '.join(_LINE_COLS)})"
    f" VALUES ($1, $2, "
    f"{', '.join(f'${i}' for i in range(3, 3 + len(_LINE_COLS)))})"
)

# line_number first ($1), then the line columns, then the WHERE keys. Scoped to
# bom_id as _DELETE_LINE_SQL is.
_LINE_UPDATE_SQL = (
    "UPDATE bom_line SET line_number = $1, "
    + ", ".join(f"{c} = ${i}" for i, c in enumerate(_LINE_COLS, start=2))
    + f" WHERE bom_line_id = ${2 + len(_LINE_COLS)}"
    + f" AND bom_id = ${3 + len(_LINE_COLS)}"
)

_ROUTE_INSERT_SQL = (
    f"INSERT INTO bom_process_route (bom_id, step_number, {', '.join(_ROUTE_COLS)})"
    f" VALUES ($1, $2, "
    f"{', '.join(f'${i}' for i in range(3, 3 + len(_ROUTE_COLS)))})"
)


# ── Validation ───────────────────────────────────────────────────────────────
#
# Split by concern so create and update share every rule but differ only in
# what is REQUIRED: create needs a name and at least one line; update needs
# whatever the caller chose to send to be sound. `partial=True` means "only
# check a key that is present".

def _pct_problem(v: Any) -> str | None:
    """Why `v` cannot go into a NUMERIC(5,3) percentage column, or None."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "must be a number"
    if not 0 <= f <= PCT_MAX:
        return f"must be between 0 and {PCT_MAX} (the column is NUMERIC(5,3))"
    return None


def _validate_identity(payload: dict, *, partial: bool) -> dict | None:
    if not partial or "fg_sku_name" in payload:
        if not _norm_name(payload.get("fg_sku_name")):
            return _err("no_sku", "fg_sku_name is required")

    entity = payload.get("entity")
    if entity is not None and entity not in ENTITIES:
        return _err("bad_entity",
                    f"entity must be one of {', '.join(ENTITIES)}",
                    entity=entity)

    bad = _pct_problem(payload.get("gst_rate"))
    if bad:
        return _err("bad_pct", f"gst_rate {bad}",
                    field="gst_rate", value=payload.get("gst_rate"))
    return None


def _validate_lines(lines: list[dict]) -> dict | None:
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
        for col in ("loss_pct", "offgrade_max_pct"):
            bad = _pct_problem(ln.get(col))
            if bad:
                return _err("bad_pct", f"Line {i}: {col} {bad}",
                            line_number=i, field=col, value=ln.get(col))
    return None


def _validate_tolerance(payload: dict, *, partial: bool) -> dict | None:
    if "allowed_balance_tolerance_pct" not in payload:
        return None
    tol = payload.get("allowed_balance_tolerance_pct")
    if tol is None:
        if partial:
            # NOT NULL in the schema. Create simply leaves the column out so the
            # default applies; an update naming it must supply a value.
            return _err("bad_tolerance",
                        "allowed_balance_tolerance_pct cannot be null; omit the "
                        "field to leave it unchanged",
                        allowed_balance_tolerance_pct=None)
        return None
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
    return None


def _validate_route(route: list[dict]) -> dict | None:
    for i, st in enumerate(route, start=1):
        if not _norm_name(st.get("process_name")):
            return _err("no_process_name", f"Route step {i}: process_name is required",
                        step_number=i)
        bad = _pct_problem(st.get("loss_pct"))
        if bad:
            return _err("bad_pct", f"Route step {i}: loss_pct {bad}",
                        step_number=i, field="loss_pct", value=st.get("loss_pct"))
    return None


def _validate(payload: dict) -> dict | None:
    """Create-shaped validation. Return an error envelope, or None when sound."""
    return (_validate_identity(payload, partial=False)
            or _validate_lines(payload.get("lines") or [])
            or _validate_tolerance(payload, partial=False)
            or _validate_route(payload.get("route") or []))


# ── Row shaping + warnings (shared by create and update) ─────────────────────

def _line_row(ln: dict) -> dict:
    row = {c: ln.get(c) for c in _LINE_COLS}
    row["material_sku_name"] = _norm_name(row["material_sku_name"])
    row["loss_pct"] = row["loss_pct"] or 0
    row["offgrade_max_pct"] = row["offgrade_max_pct"] or 0
    row["can_use_offgrade"] = bool(row["can_use_offgrade"])
    return row


def _route_row(st: dict) -> dict:
    row = {c: st.get(c) for c in _ROUTE_COLS}
    row["process_name"] = _norm_name(row["process_name"])
    # `stage` is the slug downstream code orders and matches on
    # (bar_line_service._stage_for, order_steps_packing_last). Defaulting it
    # to a slug of the process name beats storing NULL, which would make the
    # step invisible to every one of those consumers.
    row["stage"] = row["stage"] or _slug(row["process_name"])
    row["loss_pct"] = row["loss_pct"] or 0
    return row


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


def _line_warnings(lines: list[dict]) -> list[str]:
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
    return warnings


# ── Create ───────────────────────────────────────────────────────────────────

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
        row = _line_row(ln)
        await conn.execute(_LINE_INSERT_SQL, bom_id, i, *[row[c] for c in _LINE_COLS])

    route = payload.get("route") or []
    for i, st in enumerate(route, start=1):
        row = _route_row(st)
        await conn.execute(_ROUTE_INSERT_SQL, bom_id, i, *[row[c] for c in _ROUTE_COLS])

    return {
        "bom_id": bom_id,
        "fg_sku_name": fg_sku_name,
        "version": version,
        "entity": header["entity"],
        "lines_created": len(lines),
        "route_steps_created": len(route),
        "created_by": created_by,
        "warnings": _line_warnings(lines),
    }


# ── Update ───────────────────────────────────────────────────────────────────

async def update_bom(conn, bom_id: int, payload: dict, *,
                     updated_by: str | None = None) -> dict | None:
    """Partially update one BOM. MUST run inside an outer transaction.

    `payload` holds ONLY the keys the caller sent (the router dumps the body
    with exclude_unset=True), so "key present" is the signal to touch a column
    and a present key with value None writes NULL to a nullable column. Header
    columns outside _HEADER_UPDATE_COLS are ignored, never forwarded.

    `lines`, when present, REPLACES the line set matched by bom_line_id (see the
    module docstring). `route`, when present, replaces the whole route; an
    empty list clears it (nothing references route_id, so that is safe).

    Returns None when `bom_id` does not exist, an {error, message, details}
    envelope the router maps to 400/409, or:
        {bom_id, fg_sku_name, version, is_active, updated_by,
         changes: {header_fields[], lines: {inserted, updated, deleted} | None,
                   route: {steps_created} | None},
         warnings[]}
    """
    current = await conn.fetchrow(_LOCK_HEADER_SQL, bom_id)
    if current is None:
        return None

    invalid = _validate_identity(payload, partial=True)
    if not invalid and "lines" in payload:
        invalid = _validate_lines(payload.get("lines") or [])
    if not invalid:
        invalid = _validate_tolerance(payload, partial=True)
    if not invalid and "route" in payload:
        invalid = _validate_route(payload.get("route") or [])
    if invalid:
        return invalid

    # ── Header ──
    header_set: dict[str, Any] = {c: payload[c] for c in _HEADER_UPDATE_COLS
                                  if c in payload}
    if "fg_sku_name" in header_set:
        header_set["fg_sku_name"] = _norm_name(header_set["fg_sku_name"])
    if "customer_name" in header_set:
        header_set["customer_name"] = _norm_name(header_set["customer_name"]) or None

    new_name = header_set.get("fg_sku_name", current["fg_sku_name"])
    new_active = bool(header_set.get("is_active", current["is_active"]))
    name_changed = (_norm_name(new_name).lower()
                    != _norm_name(current["fg_sku_name"]).lower())
    activating = new_active and not current["is_active"]

    # Only a rename or a re-activation can create a second active header for one
    # FG, so only those pay for the probe. Same fold-both-sides rule as create;
    # this BOM is excluded so a no-op rename of an active BOM is not a 409.
    if new_active and (name_changed or activating):
        clash = await conn.fetchrow(_OTHER_ACTIVE_SQL, _norm_name(new_name), bom_id)
        if clash:
            return _err(
                "bom_exists",
                f"Another active BOM already exists for {new_name!r} "
                f"(bom_id {clash['bom_id']}, version {clash['version']}). "
                f"Deactivate it first.",
                bom_id=clash["bom_id"], version=clash["version"],
                entity=clash["entity"],
            )

    if header_set:
        cols = list(header_set)
        assigns = ", ".join(f"{c} = ${i}" for i, c in enumerate(cols, start=1))
        try:
            await conn.execute(
                f"UPDATE bom_header SET {assigns} WHERE bom_id = ${len(cols) + 1}",
                *[header_set[c] for c in cols], bom_id,
            )
        except asyncpg.UniqueViolationError:
            # The probe is check-then-write with the OTHER row unlocked, so a
            # concurrent create/re-activate can still land between them;
            # uq_bom_header_active_fg catches it and this turns the 500 into
            # the same 409 the sequential case gets.
            return _err(
                "bom_exists",
                f"Another active BOM for {new_name!r} appeared concurrently. "
                f"Reload and try again.",
                fg_sku_name=new_name,
            )

    # ── Lines ──
    line_changes = None
    warnings: list[str] = []
    if "lines" in payload:
        lines = payload.get("lines") or []
        existing_ids = {r["bom_line_id"] for r in await conn.fetch(_LINE_IDS_SQL, bom_id)}
        sent_ids = [ln.get("bom_line_id") for ln in lines
                    if ln.get("bom_line_id") is not None]
        unknown = sorted(set(sent_ids) - existing_ids)
        if unknown:
            return _err("unknown_line",
                        f"bom_line_id {unknown} do not belong to BOM {bom_id}",
                        bom_id=bom_id, bom_line_ids=unknown)
        if len(sent_ids) != len(set(sent_ids)):
            dupes = sorted({i for i in sent_ids if sent_ids.count(i) > 1})
            return _err("duplicate_line_id",
                        f"bom_line_id {dupes} appear more than once in lines",
                        bom_id=bom_id, bom_line_ids=dupes)

        to_delete = sorted(existing_ids - set(sent_ids))
        referenced: list[dict] = []
        for lid in to_delete:
            try:
                # A savepoint per delete: the FK refusal aborts only this
                # statement, so every blocked line can be collected and
                # reported together instead of one per attempt.
                async with conn.transaction():
                    await conn.execute(_DELETE_LINE_SQL, lid, bom_id)
            except asyncpg.ForeignKeyViolationError as exc:
                referenced.append({
                    "bom_line_id": lid,
                    "referenced_by": getattr(exc, "table_name", None),
                })
        if referenced:
            ids = [r["bom_line_id"] for r in referenced]
            return _err(
                "line_referenced",
                f"{len(ids)} line(s) on BOM {bom_id} are referenced by job cards "
                f"or other records and cannot be removed: bom_line_id {ids}. "
                f"Keep them on the BOM (send them back with their bom_line_id) "
                f"and change their quantities instead.",
                bom_id=bom_id, bom_line_ids=ids, references=referenced,
            )

        if sent_ids:
            await conn.execute(_PARK_LINE_NUMBERS_SQL, bom_id)

        inserted = updated = 0
        for i, ln in enumerate(lines, start=1):
            row = _line_row(ln)
            vals = [row[c] for c in _LINE_COLS]
            lid = ln.get("bom_line_id")
            if lid is None:
                await conn.execute(_LINE_INSERT_SQL, bom_id, i, *vals)
                inserted += 1
            else:
                await conn.execute(_LINE_UPDATE_SQL, i, *vals, lid, bom_id)
                updated += 1
        line_changes = {"inserted": inserted, "updated": updated,
                        "deleted": len(to_delete)}
        warnings = _line_warnings(lines)

    # ── Route ──
    route_changes = None
    if "route" in payload:
        route = payload.get("route") or []
        await conn.execute(_DELETE_ROUTE_SQL, bom_id)
        for i, st in enumerate(route, start=1):
            row = _route_row(st)
            await conn.execute(_ROUTE_INSERT_SQL, bom_id, i, *[row[c] for c in _ROUTE_COLS])
        route_changes = {"steps_created": len(route)}

    return {
        "bom_id": bom_id,
        "fg_sku_name": new_name,
        "version": current["version"],
        "is_active": new_active,
        "updated_by": updated_by,
        "changes": {
            "header_fields": list(header_set),
            "lines": line_changes,
            "route": route_changes,
        },
        "warnings": warnings,
    }


# ── Deactivate (soft delete) ─────────────────────────────────────────────────

async def deactivate_bom(conn, bom_id: int, *,
                         deactivated_by: str | None = None) -> dict | None:
    """Mark one BOM inactive. MUST run inside an outer transaction.

    Nothing is deleted — see the module docstring. Returns None when `bom_id`
    does not exist; otherwise {bom_id, fg_sku_name, version, is_active: False,
    already_inactive, deactivated_by}. Idempotent: a second call is a no-op
    that reports `already_inactive: true`.
    """
    current = await conn.fetchrow(_LOCK_HEADER_SQL, bom_id)
    if current is None:
        return None

    already_inactive = not current["is_active"]
    if not already_inactive:
        await conn.execute(_DEACTIVATE_SQL, bom_id)

    return {
        "bom_id": bom_id,
        "fg_sku_name": current["fg_sku_name"],
        "version": current["version"],
        "is_active": False,
        "already_inactive": already_inactive,
        "deactivated_by": deactivated_by,
    }


def _slug(v: str) -> str:
    """'Metal Detection' -> 'metal_detection'. Matches the shape of the slugs
    bar_line_service writes into bom_process_route.stage."""
    return "_".join("".join(c if c.isalnum() else " " for c in v).split()).lower()
