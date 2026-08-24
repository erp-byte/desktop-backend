"""Accounting CRUD — one accounting record per (job card, batch).

Backs GET/POST/PUT/DELETE /api/v1/production/accounting/record, which replace
the composite POST /job-cards-v2/{id}/outputs + PUT .../accounting/summary +
GET .../accounting trio the Accounting tab used to call.

IDENTITY
    Every call carries three 8-digit ids: job_card_id, plan_id, batch_id.
    * batch_id RESOLVES the record.
    * job_card_id must own that batch.
    * plan_id is a GUARD ONLY — validated against job_card_v2.plan_id so a
      mismatched triple 409s instead of silently editing a different card.
    Note these are the *_id columns, not the human-facing numbers:
    job_card_number is a string ('PLAN-73302918-L73302927-S1') and batch_number
    is a per-JC counter (1, 2, 3...), neither of which is an 8-digit key.

WHAT A "RECORD" IS
    One live job_card_output_v2 row (the header: output qty, kind, uom, notes),
    plus the line sections that hang off the same (job_card_id, batch_id):
        rm_consumed / pm_consumed -> job_card_material_consumption_v2
        byproducts                -> job_card_byproducts_v2
        balance_materials         -> job_card_balance_material_v2
        additives                 -> job_card_additive_consumption_v2
        qc                        -> job_card_qc_v2
    Migration 092 added deleted_at/deleted_by to all six and pinned "one live
    output row per batch" with a partial unique index.

THE SOFT-DELETE / UNIQUE-KEY TRAP (load-bearing)
    Migration 092 deliberately did NOT make the pre-existing unique indexes on
    consumption / byproducts / balance_materials partial, because
    jc_accounting_v2.py names those exact expressions as ON CONFLICT targets and
    Postgres will not match an inference clause to a partial index unless the
    statement repeats the predicate.
    So a soft-deleted row STILL OCCUPIES ITS UNIQUE KEY. Deleting "Salt" and
    re-adding it later collides with the dead row. Every insert here therefore
    RESURRECTS: `ON CONFLICT (...) DO UPDATE SET ..., deleted_at = NULL,
    deleted_by = NULL`. Miss that and re-adding a previously deleted line either
    fails or writes a row nobody can see.

UPDATE SEMANTICS
    Fetch the stored record, then compare field by field:
      * scalars     — write only the ones that actually differ
      * line arrays — match incoming to stored by natural key, then
                        same values  -> untouched (no write, no audit noise)
                        changed      -> UPDATE only the differing columns
                        missing      -> soft-delete
                        new          -> INSERT (or resurrect)
    A PUT is a FULL-RECORD replace: an omitted array means "no lines", not
    "leave alone". That is the opposite of the old POST /outputs diff-on-save
    convention (None = untouched) and is the coherent reading for CRUD.

BALANCE
    Create/Update/Delete all finish by recomputing job_card_accounting_v2
    through jc_accounting_v2.save_accounting, so is_balanced is derived from
    what was just persisted and can never be set by a client. Delete recomputes
    rather than deleting the summary, so the row always describes reality.
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.modules.production.services.job_card_v2 import assert_not_locked

logger = logging.getLogger(__name__)

# Quantity columns are NUMERIC(_,3); compare at that resolution so float noise
# (0.1 + 0.2) never registers as an operator edit.
_QTY_DP = 3

# Byproduct categories that are NOT off-grade. Mirrors _derive_accounting_payload
# in job_card_v2.py — keep the two in step.
_CTRL_SAMPLE = "control_sample"
_WASTAGE = "wastage"
_BALANCE_MATERIAL = "balance_material"


def _f(v) -> float:
    """Best-effort float coerce. None and '' both become 0.0."""
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _q(v) -> float:
    """Quantity normalised to the column's stored resolution."""
    return round(_f(v), _QTY_DP)


def _s(v) -> str | None:
    """Text normalised for comparison: blank becomes None, edges trimmed."""
    if v is None:
        return None
    t = str(v).strip()
    return t or None


def _serialize(row) -> dict:
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Identity guard
# ---------------------------------------------------------------------------

async def _resolve(conn, *, job_card_id: int, plan_id: int, batch_id: int) -> dict:
    """Validate the (job_card_id, plan_id, batch_id) triple.

    Returns an error dict the caller returns as-is, or {"jc": <row>, "batch": <row>}.
    Each mismatch is reported distinctly so the client can say WHICH id is wrong
    instead of a blanket 404.
    """
    jc = await conn.fetchrow(
        """
        SELECT job_card_id, job_card_number, plan_id, bom_id, uom, output_kind,
               status, is_locked
        FROM   job_card_v2
        WHERE  job_card_id = $1 AND deleted_at IS NULL
        """,
        job_card_id,
    )
    if not jc:
        return {"error": "job_card_not_found", "job_card_id": job_card_id}

    if int(jc["plan_id"]) != int(plan_id):
        return {
            "error": "plan_mismatch",
            "job_card_id": job_card_id,
            "expected_plan_id": int(jc["plan_id"]),
            "received_plan_id": int(plan_id),
            "message": (
                f"Job card {job_card_id} belongs to plan {jc['plan_id']}, "
                f"not {plan_id}."
            ),
        }

    batch = await conn.fetchrow(
        "SELECT batch_id, job_card_id, batch_number, status "
        "FROM job_card_batch_v2 WHERE batch_id = $1",
        batch_id,
    )
    if not batch:
        return {"error": "batch_not_found", "batch_id": batch_id}
    if int(batch["job_card_id"]) != int(job_card_id):
        return {
            "error": "batch_mismatch",
            "batch_id": batch_id,
            "batch_job_card_id": int(batch["job_card_id"]),
            "received_job_card_id": int(job_card_id),
            "message": (
                f"Batch {batch_id} belongs to job card {batch['job_card_id']}, "
                f"not {job_card_id}."
            ),
        }
    return {"jc": jc, "batch": batch}


# ---------------------------------------------------------------------------
# Section specs — one entry per line array in the payload.
#
# key:     natural-key columns used to match incoming lines to stored rows. These
#          MIRROR the table's real unique index (verified against pg_indexes);
#          diffing on anything else would let the upsert merge rows the diff
#          thought were distinct.
# values:  columns the diff compares and updates.
# conflict: the ON CONFLICT inference expression, byte-identical to the index.
# ---------------------------------------------------------------------------

_CONSUMPTION = {
    "table": "job_card_material_consumption_v2",
    "pk": "consumption_id",
    "key": ("material_sku_name",),
    # uom and issued_qty are NOT NULL with no column default, so they must be
    # supplied on every INSERT even though the CRUD payload has no field for
    # them. uom defaults to KGS (same rule as save_consumption :573); issued_qty
    # defaults to 0 because CRUD records what was CONSUMED — the issued figure
    # is owned by the RM indent flow, not by this payload.
    "values": ("actual_consumed_qty", "input_kind", "uom", "issued_qty",
               "bom_line_id", "source_dispatch_id", "remarks"),
    "conflict": "(job_card_id, COALESCE(batch_id, 0), material_sku_name)",
}
_BYPRODUCTS = {
    "table": "job_card_byproducts_v2",
    "pk": "byproduct_id",
    "key": ("category", "material_name"),
    "values": ("quantity", "uom", "bom_line_id", "remarks"),
    "conflict": "(job_card_id, COALESCE(batch_id, 0), category, COALESCE(material_name, ''))",
}
_BALANCE = {
    "table": "job_card_balance_material_v2",
    "pk": "balance_id",
    "key": ("bom_line_id", "balance_type"),
    "values": ("material_name", "material_id", "qty_kg", "uom", "remarks"),
    "conflict": "(job_card_id, COALESCE(batch_id, 0), COALESCE(bom_line_id, 0), balance_type)",
}
_ADDITIVES = {
    "table": "job_card_additive_consumption_v2",
    "pk": "additive_id",
    "key": ("sku_name", "material_name"),
    "values": ("qty_kg", "uom", "remarks"),
    # This one's index (created by migration 092) is PARTIAL, so the inference
    # clause MUST repeat the predicate or Postgres raises
    # "no unique or exclusion constraint matching the ON CONFLICT specification".
    # Consequence worth knowing: a soft-deleted additive is NOT in the partial
    # index, so re-adding it INSERTs a fresh row instead of resurrecting the dead
    # one — which is fine, and is why the other three sections (whose indexes are
    # non-partial, and therefore still hold the key while dead) are the ones that
    # actually need the deleted_at = NULL resurrection.
    "conflict": ("(job_card_id, COALESCE(batch_id, 0), "
                 "COALESCE(sku_name, material_name)) WHERE deleted_at IS NULL"),
}

# Columns compared as quantities rather than raw equality.
#
# EVERY numeric column must be listed. Postgres NUMERIC comes back as Decimal,
# so a column that falls through to the text branch compares
# str(Decimal('0.000')) == '0.000' against str(0.0) == '0.0' — never equal, and
# the diff reports a phantom change on every single save. issued_qty was missed
# on the first pass and did exactly that.
_QTY_COLS = {"actual_consumed_qty", "issued_qty", "return_qty", "variance",
             "quantity", "qty_kg", "output_qty_kg", "output_qty_units",
             "rm_consumed_kg", "process_loss_kg", "yield_pct"}


def _norm(col: str, v):
    """Normalise a value for comparison by column type."""
    if col in _QTY_COLS:
        return _q(v)
    if col in ("bom_line_id", "material_id", "source_dispatch_id"):
        return None if v is None else int(v)
    return _s(v)


def _line_key(spec: dict, row: dict) -> tuple:
    return tuple(_norm(k, row.get(k)) for k in spec["key"])


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

async def _fetch_sections(conn, job_card_id: int, batch_id: int) -> dict:
    """Live rows only, for every section of one record."""
    async def _rows(spec, extra_where=""):
        return await conn.fetch(
            f"SELECT * FROM {spec['table']} "
            f"WHERE job_card_id = $1 AND COALESCE(batch_id, 0) = COALESCE($2::bigint, 0) "
            f"AND deleted_at IS NULL {extra_where} "
            f"ORDER BY {spec['pk']}",
            job_card_id, batch_id,
        )

    consumption = await _rows(_CONSUMPTION)
    return {
        "consumption": [dict(r) for r in consumption],
        "byproducts": [dict(r) for r in await _rows(_BYPRODUCTS)],
        "balance_materials": [dict(r) for r in await _rows(_BALANCE)],
        "additives": [dict(r) for r in await _rows(_ADDITIVES)],
    }


async def _fetch_output(conn, job_card_id: int, batch_id: int):
    return await conn.fetchrow(
        """
        SELECT * FROM job_card_output_v2
        WHERE  job_card_id = $1 AND COALESCE(batch_id, 0) = COALESCE($2::bigint, 0)
          AND  deleted_at IS NULL
        """,
        job_card_id, batch_id,
    )


async def _fetch_qc(conn, job_card_id: int, batch_id: int):
    return await conn.fetchrow(
        """
        SELECT * FROM job_card_qc_v2
        WHERE  job_card_id = $1 AND COALESCE(batch_id, 0) = COALESCE($2::bigint, 0)
          AND  deleted_at IS NULL
        """,
        job_card_id, batch_id,
    )


def _shape(output, sections: dict, qc, *, job_card_id: int, plan_id: int,
           batch_id: int, summary=None) -> dict:
    """Assemble the wire shape the endpoints return."""
    o = dict(output) if output else {}
    cons = sections["consumption"]

    def _line(r, kind_filter=None):
        return {
            "bom_line_id": r.get("bom_line_id"),
            "material_sku_name": r.get("material_sku_name"),
            "consumed_qty": _f(r.get("actual_consumed_qty")),
            "input_kind": r.get("input_kind"),
            "source_dispatch_id": r.get("source_dispatch_id"),
            "remarks": r.get("remarks"),
        }

    rm = [_line(r) for r in cons if (r.get("input_kind") or "RM") != "PM"]
    pm = [_line(r) for r in cons if (r.get("input_kind") or "RM") == "PM"]

    return {
        "job_card_id": job_card_id,
        "plan_id": plan_id,
        "batch_id": batch_id,
        "output_qty_kg": _f(o.get("output_qty_kg")) if o else None,
        "output_qty_units": (_f(o.get("output_qty_units"))
                             if o.get("output_qty_units") is not None else None),
        "output_kind": o.get("output_kind"),
        "uom": o.get("uom"),
        "rm_consumed_kg": _f(o.get("rm_consumed_kg")) if o else None,
        "process_loss_kg": _f(o.get("process_loss_kg")) if o else None,
        "process_loss_remark": o.get("process_loss_remark"),
        "notes": o.get("notes"),
        "rm_consumed": rm,
        "pm_consumed": pm,
        "byproducts": [{
            "category": r.get("category"),
            "qty_kg": _f(r.get("quantity")),
            "uom": r.get("uom"),
            "material_name": r.get("material_name"),
            "bom_line_id": r.get("bom_line_id"),
            "remarks": r.get("remarks"),
        } for r in sections["byproducts"]],
        "balance_materials": [{
            "material_name": r.get("material_name"),
            "balance_type": r.get("balance_type"),
            "qty_kg": _f(r.get("qty_kg")),
            # uom (094). Deliberately NOT defaulted to KGS: an unstated unit
            # must read as unknown, or a PM line counted in pieces silently
            # becomes a weight — the defect 094 exists to end.
            "uom": _s(r.get("uom")),
            "bom_line_id": r.get("bom_line_id"),
            "material_id": r.get("material_id"),
            "remarks": r.get("remarks"),
        } for r in sections["balance_materials"]],
        "additives": [{
            "sku_name": r.get("sku_name"),
            "material_name": r.get("material_name"),
            "qty_kg": _f(r.get("qty_kg")),
            "uom": _s(r.get("uom")),        # see balance_materials.uom (094)
            "remarks": r.get("remarks"),
        } for r in sections["additives"]],
        "qc": ({
            "passed": (qc["result"] == "pass") if qc and qc["result"] else None,
            "remarks": qc["findings"] if qc else None,
            "corrective_action": qc["corrective_action"] if qc else None,
            "inspector": qc["inspector_user"] if qc else None,
        } if qc else None),
        "balance": summary,
        "recorded_by": o.get("recorded_by"),
        "recorded_at": (o["recorded_at"].isoformat()
                        if o.get("recorded_at") else None),
    }


async def get_record(conn, *, job_card_id: int, plan_id: int, batch_id: int) -> dict:
    """Read one accounting record. Live rows only."""
    guard = await _resolve(conn, job_card_id=job_card_id, plan_id=plan_id,
                           batch_id=batch_id)
    if guard.get("error"):
        return guard

    output = await _fetch_output(conn, job_card_id, batch_id)
    sections = await _fetch_sections(conn, job_card_id, batch_id)
    qc = await _fetch_qc(conn, job_card_id, batch_id)

    if output is None and not any(sections.values()):
        return {"error": "record_not_found", "job_card_id": job_card_id,
                "batch_id": batch_id,
                "message": "No accounting record exists for this batch."}

    summary = await conn.fetchrow(
        "SELECT total_input_qty, total_accounted_qty, balance_difference_qty, "
        "       is_balanced "
        "FROM   job_card_accounting_v2 "
        "WHERE  job_card_id = $1 AND COALESCE(batch_id, 0) = COALESCE($2::bigint, 0)",
        job_card_id, batch_id,
    )
    return _shape(output, sections, qc, job_card_id=job_card_id,
                  plan_id=plan_id, batch_id=batch_id,
                  summary=_serialize(summary) if summary else None)


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def _incoming_lines(payload: dict) -> dict:
    """Project the payload's five arrays onto per-table row dicts.

    rm_consumed and pm_consumed both land in job_card_material_consumption_v2 and
    are distinguished only by input_kind, so they are merged here — which is also
    why a material appearing in BOTH arrays is a caller error (same unique key).
    """
    def _cons(rows, default_kind):
        out = []
        for r in rows or []:
            out.append({
                "material_sku_name": _s(r.get("material_sku_name")),
                "actual_consumed_qty": _q(r.get("consumed_qty")),
                "input_kind": _s(r.get("input_kind")) or default_kind,
                # NOT NULL, no default — see _CONSUMPTION["values"].
                "uom": (_s(r.get("uom")) or "KGS").upper(),
                "issued_qty": _q(r.get("issued_qty")),
                "bom_line_id": r.get("bom_line_id"),
                "source_dispatch_id": r.get("source_dispatch_id"),
                "remarks": _s(r.get("remarks")),
            })
        return out

    return {
        "consumption": _cons(payload.get("rm_consumed"), "RM")
                       + _cons(payload.get("pm_consumed"), "PM"),
        "byproducts": [{
            "category": _s(r.get("category")),
            "quantity": _q(r.get("qty_kg")),
            "uom": _s(r.get("uom")) or "KGS",
            "material_name": _s(r.get("material_name")),
            "bom_line_id": r.get("bom_line_id"),
            "remarks": _s(r.get("remarks")),
        } for r in payload.get("byproducts") or []],
        "balance_materials": [{
            "material_name": _s(r.get("material_name")),
            "balance_type": _s(r.get("balance_type")),
            "qty_kg": _q(r.get("qty_kg")),
            "uom": _s(r.get("uom")),        # 094; None = unit not stated
            "bom_line_id": r.get("bom_line_id"),
            "material_id": r.get("material_id"),
            "remarks": _s(r.get("remarks")),
        } for r in payload.get("balance_materials") or []],
        "additives": [{
            "sku_name": _s(r.get("sku_name")),
            "material_name": _s(r.get("material_name")),
            "qty_kg": _q(r.get("qty_kg")),
            "uom": _s(r.get("uom")),        # 094; None = unit not stated
            "remarks": _s(r.get("remarks")),
        } for r in payload.get("additives") or []],
    }


def _validate_lines(incoming: dict) -> dict | None:
    """Reject payloads whose lines collide on their own natural key.

    Two rm_consumed rows naming the same material would silently merge in the
    upsert — the operator would see one row swallow the other with no error. A
    400 up front is the honest answer.
    """
    for section, spec in (("consumption", _CONSUMPTION),
                          ("byproducts", _BYPRODUCTS),
                          ("balance_materials", _BALANCE),
                          ("additives", _ADDITIVES)):
        seen: dict[tuple, int] = {}
        for i, row in enumerate(incoming[section]):
            k = _line_key(spec, row)
            if all(part is None for part in k):
                return {
                    "error": "invalid_line",
                    "section": section,
                    "index": i,
                    "message": (
                        f"{section}[{i}] has no identifying value "
                        f"({', '.join(spec['key'])} all empty)."
                    ),
                }
            if k in seen:
                return {
                    "error": "duplicate_line",
                    "section": section,
                    "index": i,
                    "first_index": seen[k],
                    "key": [str(p) for p in k],
                    "message": (
                        f"{section}[{i}] repeats the key {list(k)} already used by "
                        f"{section}[{seen[k]}]. Merge them into one line."
                    ),
                }
            seen[k] = i

    # NOT NULL columns the payload can leave empty. Caught here so the caller
    # gets a named field instead of a raw NotNullViolationError from asyncpg.
    for i, row in enumerate(incoming["balance_materials"]):
        if not row.get("material_name"):
            return {
                "error": "invalid_line",
                "section": "balance_materials",
                "index": i,
                "field": "material_name",
                "message": f"balance_materials[{i}].material_name is required.",
            }
    for i, row in enumerate(incoming["consumption"]):
        if not row.get("material_sku_name"):
            return {
                "error": "invalid_line",
                "section": "consumption",
                "index": i,
                "field": "material_sku_name",
                "message": (
                    f"consumption[{i}].material_sku_name is required — it is the "
                    "line's identity key."
                ),
            }
    for i, row in enumerate(incoming["byproducts"]):
        if not row.get("category"):
            return {
                "error": "invalid_line",
                "section": "byproducts",
                "index": i,
                "field": "category",
                "message": f"byproducts[{i}].category is required.",
            }
    return None


async def _upsert_section(conn, spec: dict, *, job_card_id: int, batch_id: int,
                          incoming: list[dict], stored: list[dict],
                          actor: str | None) -> dict:
    """Per-line diff for one section. Returns a change tally.

    RESURRECTION: the ON CONFLICT DO UPDATE clears deleted_at, because a
    soft-deleted row still holds the unique key (see module docstring).
    """
    tally = {"inserted": 0, "updated": 0, "deleted": 0, "unchanged": 0,
             "changes": []}

    stored_by_key = {_line_key(spec, r): r for r in stored}
    incoming_by_key = {_line_key(spec, r): r for r in incoming}

    # ── changed / new ────────────────────────────────────────────────────────
    for key, new_row in incoming_by_key.items():
        old = stored_by_key.get(key)
        if old is not None:
            diffs = {c: new_row.get(c) for c in spec["values"]
                     if _norm(c, new_row.get(c)) != _norm(c, old.get(c))}
            if not diffs:
                tally["unchanged"] += 1
                continue
            sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(diffs))
            await conn.execute(
                f"UPDATE {spec['table']} SET {sets} WHERE {spec['pk']} = $1",
                old[spec["pk"]], *diffs.values(),
            )
            tally["updated"] += 1
            for c, v in diffs.items():
                tally["changes"].append({
                    "section": spec["table"], "key": [str(p) for p in key],
                    "field": c, "before": _serialize({"v": old.get(c)})["v"],
                    "after": v,
                })
            continue

        cols = ["job_card_id", "batch_id", *spec["key"], *spec["values"]]
        # Some tables carry recorded_by; all of ours do.
        cols.append("recorded_by")
        vals = [job_card_id, batch_id,
                *[new_row.get(k) for k in spec["key"]],
                *[new_row.get(c) for c in spec["values"]],
                actor]
        placeholders = ", ".join(f"${i + 2}" for i in range(len(cols)))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in spec["values"])

        async def _ins(_cols=cols, _vals=vals, _ph=placeholders, _up=updates):
            return await conn.fetchrow(
                f"""
                INSERT INTO {spec['table']} ({spec['pk']}, {', '.join(_cols)})
                VALUES ($1, {_ph})
                ON CONFLICT {spec['conflict']} DO UPDATE SET
                    {_up},
                    batch_id   = EXCLUDED.batch_id,
                    deleted_at = NULL,
                    deleted_by = NULL
                RETURNING {spec['pk']}
                """,
                new_short_time_id(), *_vals,
            )

        await insert_with_pk_retry(conn, _ins)
        tally["inserted"] += 1
        tally["changes"].append({
            "section": spec["table"], "key": [str(p) for p in key],
            "field": "*", "before": None, "after": "inserted",
        })

    # ── dropped ──────────────────────────────────────────────────────────────
    for key, old in stored_by_key.items():
        if key in incoming_by_key:
            continue
        await conn.execute(
            f"UPDATE {spec['table']} SET deleted_at = NOW(), deleted_by = $2 "
            f"WHERE {spec['pk']} = $1",
            old[spec["pk"]], actor,
        )
        tally["deleted"] += 1
        tally["changes"].append({
            "section": spec["table"], "key": [str(p) for p in key],
            "field": "*", "before": "live", "after": "deleted",
        })

    return tally


_OUTPUT_SCALARS = ("output_qty_kg", "output_qty_units", "output_kind", "uom",
                   "rm_consumed_kg", "process_loss_kg", "process_loss_remark",
                   "notes")


def _output_values(payload: dict, jc) -> dict:
    return {
        "output_qty_kg": _q(payload.get("output_qty_kg")),
        "output_qty_units": (_q(payload.get("output_qty_units"))
                             if payload.get("output_qty_units") is not None else None),
        "output_kind": _s(payload.get("output_kind")) or jc["output_kind"],
        "uom": (_s(payload.get("uom")) or jc["uom"] or "KGS").upper(),
        "rm_consumed_kg": _q(payload.get("rm_consumed_kg")),
        "process_loss_kg": _q(payload.get("process_loss_kg")),
        "process_loss_remark": _s(payload.get("process_loss_remark")),
        "notes": _s(payload.get("notes")),
    }


async def _upsert_qc(conn, *, job_card_id: int, batch_id: int,
                     qc: dict | None, actor: str | None) -> bool:
    """Write / clear the per-batch QC row. Returns True when it changed."""
    stored = await _fetch_qc(conn, job_card_id, batch_id)
    if qc is None:
        if stored is None:
            return False
        await conn.execute(
            "UPDATE job_card_qc_v2 SET deleted_at = NOW(), deleted_by = $2 "
            "WHERE qc_id = $1",
            stored["qc_id"], actor,
        )
        return True

    result = "pass" if qc.get("passed") else "fail"
    findings = _s(qc.get("remarks"))
    corrective = _s(qc.get("corrective_action"))
    inspector = _s(qc.get("inspector")) or actor

    if stored is not None:
        same = (stored["result"] == result
                and _s(stored["findings"]) == findings
                and _s(stored["corrective_action"]) == corrective
                and _s(stored["inspector_user"]) == inspector)
        if same:
            return False

    async def _ins():
        return await conn.fetchrow(
            """
            INSERT INTO job_card_qc_v2
                (qc_id, job_card_id, batch_id, result, findings,
                 corrective_action, inspector_user, recorded_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (job_card_id, COALESCE(batch_id, 0)) DO UPDATE SET
                result            = EXCLUDED.result,
                findings          = EXCLUDED.findings,
                corrective_action = EXCLUDED.corrective_action,
                inspector_user    = EXCLUDED.inspector_user,
                deleted_at        = NULL,
                deleted_by        = NULL,
                updated_at        = NOW()
            RETURNING qc_id
            """,
            new_short_time_id(), job_card_id, batch_id, result, findings,
            corrective, inspector, actor,
        )

    await insert_with_pk_retry(conn, _ins)
    return True


# ---------------------------------------------------------------------------
# Balance recompute
# ---------------------------------------------------------------------------

def _summary_payload(record_sections: dict, output_vals: dict) -> dict:
    """Build the AccountingSummaryRequest shape from the persisted record.

    Mirrors _derive_accounting_payload in job_card_v2.py — off-grade is every
    byproduct category except control_sample / wastage / balance_material / pm_*,
    and rejection collapses into off-grade per operator policy — but scoped to
    ONE batch rather than the whole JC.

    total_input is the RM-side consumption sum (PM is packaging, counted in
    pieces, and must never enter the kg identity).
    """
    total_input = sum(_f(r.get("actual_consumed_qty"))
                      for r in record_sections["consumption"]
                      if (r.get("input_kind") or "RM") != "PM")

    offgrade = wastage = control_sample = 0.0
    for r in record_sections["byproducts"]:
        cat = (r.get("category") or "").strip()
        qty = _f(r.get("quantity"))
        if cat == _CTRL_SAMPLE:
            control_sample += qty
        elif cat == _WASTAGE:
            wastage += qty
        elif cat == _BALANCE_MATERIAL or cat.startswith("pm_"):
            continue
        else:
            offgrade += qty

    balance_material = extra_give = 0.0
    for r in record_sections["balance_materials"]:
        bt = (r.get("balance_type") or "").strip()
        qty = _f(r.get("qty_kg"))
        if bt == "returned":
            balance_material += qty
        elif bt == "extra_given":
            extra_give += qty

    return {
        "total_input_qty": total_input,
        "input_uom": output_vals.get("uom") or "KGS",
        "output_qty": _f(output_vals.get("output_qty_kg")),
        "output_uom": output_vals.get("uom") or "KGS",
        "output_qty_units": output_vals.get("output_qty_units"),
        "process_loss_qty": _f(output_vals.get("process_loss_kg")),
        "process_loss_breakdown": None,
        "extra_give_away_qty": extra_give,
        "balance_material_qty": balance_material,
        "offgrade_total_qty": offgrade,
        "rejection_qty": 0.0,
        "wastage_qty": wastage,
        "control_sample_qty": control_sample,
    }


async def _recompute_summary(conn, *, job_card_id: int, batch_id: int,
                             actor: str | None) -> dict:
    """Re-derive job_card_accounting_v2 from the CURRENTLY LIVE record.

    Reused by create / update / delete so the summary can never describe rows
    that no longer exist. Delegates the actual maths to
    jc_accounting_v2.save_accounting, which owns the R9 identity — including the
    fix that keeps dispatched_out OUT of the OUT side.
    """
    from app.modules.production.services.jc_accounting_v2 import save_accounting

    sections = await _fetch_sections(conn, job_card_id, batch_id)
    output = await _fetch_output(conn, job_card_id, batch_id)
    output_vals = {
        "uom": output["uom"] if output else "KGS",
        "output_qty_kg": output["output_qty_kg"] if output else 0,
        "output_qty_units": output["output_qty_units"] if output else None,
        "process_loss_kg": output["process_loss_kg"] if output else 0,
    }
    payload = _summary_payload(sections, output_vals)
    return await save_accounting(
        conn, job_card_id=job_card_id, payload=payload,
        saved_by=actor, batch_id=batch_id,
    )


def _balance_block(save_result: dict) -> dict | None:
    if not save_result or save_result.get("error"):
        return None
    return {
        "total_accounted_qty": save_result.get("total_accounted_qty"),
        "balance_difference_qty": save_result.get("balance_difference_qty"),
        "is_balanced": save_result.get("is_balanced"),
        "tolerance_pct": save_result.get("tolerance_pct"),
    }


# ---------------------------------------------------------------------------
# Create / Update / Delete
# ---------------------------------------------------------------------------

async def _write(conn, *, job_card_id: int, plan_id: int, batch_id: int,
                 payload: dict, actor: str | None, jc, creating: bool) -> dict:
    """Shared body for create and update. `creating` only changes the messages."""
    incoming = _incoming_lines(payload)
    bad = _validate_lines(incoming)
    if bad:
        return bad

    stored_sections = await _fetch_sections(conn, job_card_id, batch_id)
    stored_output = await _fetch_output(conn, job_card_id, batch_id)
    out_vals = _output_values(payload, jc)

    scalar_changes = []
    if stored_output is None:
        cols = ["job_card_id", "batch_id", *_OUTPUT_SCALARS, "recorded_by"]
        vals = [job_card_id, batch_id, *[out_vals[c] for c in _OUTPUT_SCALARS], actor]
        ph = ", ".join(f"${i + 2}" for i in range(len(cols)))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _OUTPUT_SCALARS)

        async def _ins():
            return await conn.fetchrow(
                f"""
                INSERT INTO job_card_output_v2 (output_id, {', '.join(cols)})
                VALUES ($1, {ph})
                ON CONFLICT (job_card_id, COALESCE(batch_id, 0))
                    WHERE deleted_at IS NULL
                DO UPDATE SET {updates}, deleted_at = NULL, deleted_by = NULL
                RETURNING output_id
                """,
                new_short_time_id(), *vals,
            )

        await insert_with_pk_retry(conn, _ins)
        scalar_changes.append({"field": "*", "before": None, "after": "inserted"})
    else:
        diffs = {c: out_vals[c] for c in _OUTPUT_SCALARS
                 if _norm(c, out_vals[c]) != _norm(c, stored_output[c])}
        if diffs:
            sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(diffs))
            await conn.execute(
                f"UPDATE job_card_output_v2 SET {sets} WHERE output_id = $1",
                stored_output["output_id"], *diffs.values(),
            )
            for c, v in diffs.items():
                scalar_changes.append({
                    "field": c,
                    "before": _serialize({"v": stored_output[c]})["v"],
                    "after": v,
                })

    tallies = {}
    for name, spec in (("consumption", _CONSUMPTION),
                       ("byproducts", _BYPRODUCTS),
                       ("balance_materials", _BALANCE),
                       ("additives", _ADDITIVES)):
        tallies[name] = await _upsert_section(
            conn, spec, job_card_id=job_card_id, batch_id=batch_id,
            incoming=incoming[name], stored=stored_sections[name], actor=actor,
        )

    qc_changed = await _upsert_qc(conn, job_card_id=job_card_id,
                                 batch_id=batch_id, qc=payload.get("qc"),
                                 actor=actor)

    save_result = await _recompute_summary(conn, job_card_id=job_card_id,
                                           batch_id=batch_id, actor=actor)
    if save_result.get("error"):
        return {"error": "summary_failed", "underlying": save_result,
                "message": "Record written but the balance summary could not be "
                           "recomputed."}

    record = await get_record(conn, job_card_id=job_card_id, plan_id=plan_id,
                              batch_id=batch_id)
    return {
        "created" if creating else "updated": True,
        "record": record,
        "changes": {
            "output": scalar_changes,
            **{k: {kk: vv for kk, vv in v.items() if kk != "changes"}
               for k, v in tallies.items()},
            "qc_changed": qc_changed,
            "detail": [c for t in tallies.values() for c in t["changes"]],
        },
    }


async def create_record(conn, *, job_card_id: int, plan_id: int, batch_id: int,
                        payload: dict, actor: str | None = None,
                        admin_override: bool = False) -> dict:
    """Create the accounting record for a batch. 409s if one already exists."""
    guard = await _resolve(conn, job_card_id=job_card_id, plan_id=plan_id,
                           batch_id=batch_id)
    if guard.get("error"):
        return guard
    lock_err = await assert_not_locked(conn, job_card_id)
    if lock_err:
        return lock_err

    if guard["batch"]["status"] != "open" and not admin_override:
        return {
            "error": "batch_not_open",
            "batch_id": batch_id,
            "status": guard["batch"]["status"],
            "message": (
                f"Batch {batch_id} is '{guard['batch']['status']}'. Creating an "
                "accounting record against it needs admin_override."
            ),
        }

    existing = await _fetch_output(conn, job_card_id, batch_id)
    if existing is not None:
        return {
            "error": "record_exists",
            "job_card_id": job_card_id,
            "batch_id": batch_id,
            "message": "An accounting record already exists for this batch. "
                       "Use PUT to update it.",
        }

    return await _write(conn, job_card_id=job_card_id, plan_id=plan_id,
                        batch_id=batch_id, payload=payload, actor=actor,
                        jc=guard["jc"], creating=True)


async def update_record(conn, *, job_card_id: int, plan_id: int, batch_id: int,
                        payload: dict, actor: str | None = None,
                        admin_override: bool = False) -> dict:
    """Update the record, writing only the fields that actually differ."""
    guard = await _resolve(conn, job_card_id=job_card_id, plan_id=plan_id,
                           batch_id=batch_id)
    if guard.get("error"):
        return guard
    lock_err = await assert_not_locked(conn, job_card_id)
    if lock_err:
        return lock_err

    if guard["batch"]["status"] == "cancelled" and not admin_override:
        return {"error": "batch_cancelled", "batch_id": batch_id,
                "message": "Batch is cancelled; editing needs admin_override."}

    existing = await _fetch_output(conn, job_card_id, batch_id)
    sections = await _fetch_sections(conn, job_card_id, batch_id)
    if existing is None and not any(sections.values()):
        return {"error": "record_not_found", "job_card_id": job_card_id,
                "batch_id": batch_id,
                "message": "No accounting record to update. Use POST to create it."}

    return await _write(conn, job_card_id=job_card_id, plan_id=plan_id,
                        batch_id=batch_id, payload=payload, actor=actor,
                        jc=guard["jc"], creating=False)


async def delete_record(conn, *, job_card_id: int, plan_id: int, batch_id: int,
                        actor: str | None = None,
                        admin_override: bool = False) -> dict:
    """Soft-delete the whole record, then RECOMPUTE the summary.

    The summary row is deliberately kept (and re-derived from the now-empty
    record) rather than deleted, so job_card_accounting_v2 always describes rows
    that actually exist.

    NOTE for the caller: with every line gone, total_input becomes 0, which puts
    save_accounting on its absolute-tolerance branch (|diff| <= 0.05 kg). An
    empty record therefore computes as BALANCED, and the R9 close gate would let
    the JC close on a record that was just deleted. The endpoint layer guards
    this by refusing to delete once the JC is past 'in_progress'.
    """
    guard = await _resolve(conn, job_card_id=job_card_id, plan_id=plan_id,
                           batch_id=batch_id)
    if guard.get("error"):
        return guard
    lock_err = await assert_not_locked(conn, job_card_id)
    if lock_err:
        return lock_err

    if guard["jc"]["status"] in ("completed", "closed") and not admin_override:
        return {
            "error": "job_card_closed",
            "status": guard["jc"]["status"],
            "message": (
                f"Job card is '{guard['jc']['status']}'. Deleting its accounting "
                "record would leave a closed card with no figures; needs "
                "admin_override."
            ),
        }

    existing = await _fetch_output(conn, job_card_id, batch_id)
    sections = await _fetch_sections(conn, job_card_id, batch_id)
    if existing is None and not any(sections.values()):
        return {"error": "record_not_found", "job_card_id": job_card_id,
                "batch_id": batch_id}

    deleted = {}
    for name, spec in (("consumption", _CONSUMPTION),
                       ("byproducts", _BYPRODUCTS),
                       ("balance_materials", _BALANCE),
                       ("additives", _ADDITIVES)):
        n = await conn.fetchval(
            f"""
            WITH gone AS (
                UPDATE {spec['table']}
                   SET deleted_at = NOW(), deleted_by = $3
                 WHERE job_card_id = $1
                   AND COALESCE(batch_id, 0) = COALESCE($2::bigint, 0)
                   AND deleted_at IS NULL
                RETURNING 1
            )
            SELECT COUNT(*) FROM gone
            """,
            job_card_id, batch_id, actor,
        )
        deleted[name] = int(n or 0)

    for table, col in (("job_card_output_v2", "output"), ("job_card_qc_v2", "qc")):
        n = await conn.fetchval(
            f"""
            WITH gone AS (
                UPDATE {table}
                   SET deleted_at = NOW(), deleted_by = $3
                 WHERE job_card_id = $1
                   AND COALESCE(batch_id, 0) = COALESCE($2::bigint, 0)
                   AND deleted_at IS NULL
                RETURNING 1
            )
            SELECT COUNT(*) FROM gone
            """,
            job_card_id, batch_id, actor,
        )
        deleted[col] = int(n or 0)

    save_result = await _recompute_summary(conn, job_card_id=job_card_id,
                                           batch_id=batch_id, actor=actor)

    # ── Close the "empty record reads as balanced" hole ──────────────────────
    # save_accounting picks its tolerance branch on total_input: when input is 0
    # it falls back to the ABSOLUTE check (|diff| <= 0.05 kg). An emptied record
    # has input 0 and output 0, so diff is 0 and it computes as BALANCED — which
    # would let the R9 close gate pass a job card whose figures were just
    # deleted. Nothing else catches this: the gate only reads is_balanced.
    #
    # A record with no live rows is not "balanced", it is ABSENT. Stamp the
    # verdict false so the gate refuses until the record is re-created.
    still_live = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM job_card_material_consumption_v2
             WHERE job_card_id = $1 AND COALESCE(batch_id, 0) = COALESCE($2::bigint, 0)
               AND deleted_at IS NULL
            UNION ALL
            SELECT 1 FROM job_card_output_v2
             WHERE job_card_id = $1 AND COALESCE(batch_id, 0) = COALESCE($2::bigint, 0)
               AND deleted_at IS NULL
        )
        """,
        job_card_id, batch_id,
    )
    if not still_live:
        await conn.execute(
            """
            UPDATE job_card_accounting_v2
               SET is_balanced = FALSE
             WHERE job_card_id = $1
               AND COALESCE(batch_id, 0) = COALESCE($2::bigint, 0)
            """,
            job_card_id, batch_id,
        )
        if save_result and not save_result.get("error"):
            save_result = {**save_result, "is_balanced": False}

    return {
        "deleted": True,
        "job_card_id": job_card_id,
        "plan_id": plan_id,
        "batch_id": batch_id,
        "rows_soft_deleted": deleted,
        "balance": _balance_block(save_result),
        "record_empty": not still_live,
    }
