"""R8 maker-checker service for v2.

Handles the 12-row amendment matrix:
    one_off_material_add, one_off_material_remove,
    permanent_bom_add, permanent_bom_remove, permanent_bom_qty_change,
    plan_delete, stop_process, force_unlock,
    uom_correction, ncr_disposition,
    unbalanced_close_override, ega_override.

Lifecycle:
    draft -> pending_review -> pending_final -> approved -> applied
                            -> rejected
                            -> withdrawn (only from draft / pending_review)

Apply step is per-type and runs inside the same txn as the final
approval. If the apply step fails it RAISES, which aborts the outer
asyncpg transaction owned by the router, rolling back both the
status flip to 'approved' and any partial side-effects. The amendment
stays at its prior status ('pending_review' or 'pending_final') so
the operator can fix the upstream cause and re-approve. Tracked in
bom_amendment_request_v2.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal

from app.core.helpers import insert_with_pk_retry, new_short_time_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request-type metadata
# ---------------------------------------------------------------------------

# Per the R8 matrix in the framework spec. The list is the source of truth
# for what request_types the create endpoint accepts and which apply
# branches the approve endpoint dispatches into.
REQUEST_TYPES = frozenset({
    'one_off_material_add',
    'one_off_material_remove',
    'permanent_bom_add',
    'permanent_bom_remove',
    'permanent_bom_qty_change',
    'plan_delete',
    'plan_field_change',
    'stop_process',
    'force_unlock',
    'uom_correction',
    'ncr_disposition',
    'unbalanced_close_override',
    'ega_override',
})

# Plan-field amendment scope: which top-level fields and per-line fields
# may be patched. Anything outside these allow-lists is rejected at
# validation time so an amendment can't smuggle in changes to fields
# that aren't supposed to be operator-editable on an approved plan.
_PLAN_FIELD_KEYS = frozenset({'plan_date', 'date_from', 'date_to', 'plan_type'})
_PLAN_LINE_FIELD_KEYS = frozenset({
    'planned_qty_kg', 'planned_qty_units', 'area', 'deadline_date',
})
# JC statuses that accept the qty/deadline cascade. Past this set, work
# has begun and a silent push of new plan values would corrupt running
# accounting / variance.
_PLAN_CASCADE_JC_STATUSES = ('locked', 'unlocked', 'assigned')

# Status set, lifted from the DB CHECK on bom_amendment_request_v2.
STATUS_DRAFT          = 'draft'
STATUS_PENDING_REVIEW = 'pending_review'
STATUS_PENDING_FINAL  = 'pending_final'
STATUS_APPROVED       = 'approved'
STATUS_APPLIED        = 'applied'
STATUS_REJECTED       = 'rejected'
STATUS_WITHDRAWN      = 'withdrawn'

TERMINAL_STATUSES = frozenset({STATUS_APPLIED, STATUS_REJECTED, STATUS_WITHDRAWN})

# Which request types need a SECOND checker (rather than going directly
# from pending_review -> approved on the first approval). Drives whether
# the first approve() advances to pending_final or to approved.
TWO_CHECKER_TYPES = frozenset({
    'permanent_bom_add',
    'permanent_bom_remove',
    'permanent_bom_qty_change',
    # ncr_disposition only escalates to a 2nd checker when scrap;
    # encoded in routing logic, not here.
})


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

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
# Create
# ---------------------------------------------------------------------------

async def create_amendment(conn, *, request_type: str,
                           payload: dict,
                           maker_user_id: int,
                           job_card_id: int | None = None,
                           bom_id: int | None = None,
                           reason: str | None = None) -> dict:
    """Open a new amendment in pending_review (the maker has already
    committed to submitting). Returns the inserted row.

    Optional `bom_id` / `job_card_id` reflect what the amendment targets.
    The router resolves these from the maker's UI selection.

    `maker_user_id` is REQUIRED (no default). The self-approval and
    maker-only-withdraw checks downstream all depend on a known maker,
    so we refuse to create an orphaned request. See H1 in B11 review.
    """
    if request_type not in REQUEST_TYPES:
        return {
            "error": "invalid_request_type",
            "request_type": request_type,
            "allowed": sorted(REQUEST_TYPES),
        }
    if maker_user_id is None:
        return {
            "error": "maker_unknown",
            "message": "maker_user_id is required to create an amendment.",
        }
    if not isinstance(payload, dict):
        return {"error": "invalid_payload", "message": "payload must be a JSON object"}
    # Light shape sanity per request_type. Full validation lives in the
    # router's Pydantic layer; this is defence-in-depth.
    payload_error = _validate_payload(request_type, payload)
    if payload_error:
        return payload_error

    async def _insert():
        return await conn.fetchrow(
            """
            INSERT INTO bom_amendment_request_v2 (
                request_id, request_type, bom_id, job_card_id,
                payload_jsonb, maker_user_id, status, reason,
                created_at
            ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, NOW())
            RETURNING *
            """,
            new_short_time_id(),
            request_type, bom_id, job_card_id,
            json.dumps(payload),
            maker_user_id, STATUS_PENDING_REVIEW, reason,
        )

    row = await insert_with_pk_retry(conn, _insert)
    return {"created": True, "amendment": _serialize(row)}


# Per-kg / per-pcs / per-unit UoM groupings used for conditional-required
# checks. PCS-family materials need a qty_pcs in addition to qty_kg.
_PCS_UOMS = frozenset({'PCS', 'NOS', 'ROLL', 'SETS', 'BUNDLE'})

# Allowed NCR disposition outcomes (H4(c)). Mirrors qc module semantics.
_NCR_DISPOSITIONS = frozenset({'use_as_is', 'rework', 'scrap'})


def _validate_payload(request_type: str, payload: dict) -> dict | None:
    """Defense-in-depth payload shape & value sanity. Returns a structured
    error dict on failure or None on success. Bundles the per-type
    required-keys check, conditional-required keys (e.g. qty_pcs when
    uom is PCS-family), positive-number checks, and enum constraints.
    """
    required = {
        'one_off_material_add':       ('material_sku_name', 'qty_kg', 'uom'),
        'one_off_material_remove':    ('material_sku_name',),
        'permanent_bom_add':          ('material_sku_name', 'qty_per_unit', 'uom', 'item_type'),
        'permanent_bom_remove':       ('bom_line_id',),
        'permanent_bom_qty_change':   ('bom_line_id', 'new_qty'),
        'plan_delete':                ('plan_id',),
        'plan_field_change':          ('plan_id',),
        'stop_process':               ('reason',),
        'force_unlock':               ('reason',),
        'uom_correction':             ('sku_id', 'new_uom_kg'),
        'ncr_disposition':            ('output_id', 'disposition'),
        'unbalanced_close_override':  ('balance_diff_kg',),
        'ega_override':               ('material_sku_name', 'ega_kg'),
    }.get(request_type, ())
    for k in required:
        if k not in payload:
            return {
                "error":   "payload_missing_key",
                "request_type": request_type,
                "missing": k,
                "message": (
                    f"payload for '{request_type}' must include '{k}' - "
                    "see migration 030 payload-shape documentation."
                ),
            }

    # H4(a): one_off_material_add - PCS-family uom requires qty_pcs.
    if request_type == 'one_off_material_add':
        uom = (payload.get('uom') or '').strip().upper()
        if uom in _PCS_UOMS and payload.get('qty_pcs') is None:
            return {
                "error":   "payload_missing_key",
                "request_type": request_type,
                "missing": "qty_pcs",
                "message": (
                    f"uom '{uom}' requires qty_pcs in addition to qty_kg."
                ),
            }

    # H4(b): non-positive quantity rejection for the obvious magnitude keys.
    POSITIVE_KEYS = ('qty_kg', 'qty_pcs', 'ega_kg', 'new_qty',
                     'qty_per_unit', 'new_uom_kg')
    for k in POSITIVE_KEYS:
        if k in payload and payload[k] is not None:
            try:
                v = float(payload[k])
            except (TypeError, ValueError):
                return {
                    "error":   "payload_invalid_value",
                    "request_type": request_type,
                    "field":   k,
                    "message": f"'{k}' must be numeric.",
                }
            if v <= 0:
                return {
                    "error":   "payload_invalid_value",
                    "request_type": request_type,
                    "field":   k,
                    "message": f"'{k}' must be > 0 (got {v}).",
                }

    # H4(c): ncr_disposition enum constraint.
    if request_type == 'ncr_disposition':
        disp = payload.get('disposition')
        if disp not in _NCR_DISPOSITIONS:
            return {
                "error":   "payload_invalid_value",
                "request_type": request_type,
                "field":   "disposition",
                "message": (
                    f"'disposition' must be one of "
                    f"{sorted(_NCR_DISPOSITIONS)} (got {disp!r})."
                ),
            }

    # plan_field_change: must specify at least one editable scope, and
    # every supplied key must be in the field allow-list.
    if request_type == 'plan_field_change':
        plan_fields = payload.get('plan_fields') or {}
        line_changes = payload.get('line_changes') or []
        if not isinstance(plan_fields, dict):
            return {
                "error":   "payload_invalid_value",
                "request_type": request_type,
                "field":   "plan_fields",
                "message": "'plan_fields' must be an object.",
            }
        if not isinstance(line_changes, list):
            return {
                "error":   "payload_invalid_value",
                "request_type": request_type,
                "field":   "line_changes",
                "message": "'line_changes' must be a list.",
            }
        if not plan_fields and not line_changes:
            return {
                "error":   "payload_invalid_value",
                "request_type": request_type,
                "field":   "plan_fields|line_changes",
                "message": (
                    "plan_field_change must specify at least one of "
                    "plan_fields or line_changes (nothing to apply)."
                ),
            }
        bad = [k for k in plan_fields if k not in _PLAN_FIELD_KEYS]
        if bad:
            return {
                "error":   "payload_invalid_value",
                "request_type": request_type,
                "field":   "plan_fields",
                "message": (
                    f"plan_fields keys not in allow-list: {bad}. "
                    f"Allowed: {sorted(_PLAN_FIELD_KEYS)}."
                ),
            }
        for i, lc in enumerate(line_changes):
            if not isinstance(lc, dict) or lc.get('plan_line_id') is None:
                return {
                    "error":   "payload_invalid_value",
                    "request_type": request_type,
                    "field":   f"line_changes[{i}].plan_line_id",
                    "message": "each line_change must include plan_line_id.",
                }
            fields = lc.get('fields') or {}
            if not isinstance(fields, dict) or not fields:
                return {
                    "error":   "payload_invalid_value",
                    "request_type": request_type,
                    "field":   f"line_changes[{i}].fields",
                    "message": "each line_change.fields must be a non-empty object.",
                }
            bad_line = [k for k in fields if k not in _PLAN_LINE_FIELD_KEYS]
            if bad_line:
                return {
                    "error":   "payload_invalid_value",
                    "request_type": request_type,
                    "field":   f"line_changes[{i}].fields",
                    "message": (
                        f"fields not in allow-list: {bad_line}. "
                        f"Allowed: {sorted(_PLAN_LINE_FIELD_KEYS)}."
                    ),
                }

    return None


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

async def list_amendments(conn, *,
                          status: str | None = None,
                          request_type: str | None = None,
                          job_card_id: int | None = None,
                          bom_id: int | None = None,
                          mine_user_id: int | None = None,
                          page: int = 1,
                          page_size: int = 50) -> dict:
    """List amendments. `mine_user_id` filters to rows where the caller
    is either the maker, checker1, or checker2 - matches the in-app
    "my approvals" inbox semantic.
    """
    conditions: list[str] = []
    params: list = []
    idx = 1
    if status:
        statuses = [s.strip() for s in status.split(',') if s.strip()]
        if statuses:
            ph = ', '.join(f'${idx + i}' for i in range(len(statuses)))
            conditions.append(f"status IN ({ph})")
            params.extend(statuses); idx += len(statuses)
    if request_type:
        conditions.append(f"request_type = ${idx}"); params.append(request_type); idx += 1
    if job_card_id is not None:
        conditions.append(f"job_card_id = ${idx}"); params.append(job_card_id); idx += 1
    if bom_id is not None:
        conditions.append(f"bom_id = ${idx}"); params.append(bom_id); idx += 1
    if mine_user_id is not None:
        conditions.append(
            f"(maker_user_id = ${idx} OR checker1_user_id = ${idx} OR checker2_user_id = ${idx})"
        )
        params.append(mine_user_id); idx += 1

    where = " AND ".join(conditions) if conditions else "TRUE"
    offset = (page - 1) * page_size
    total = await conn.fetchval(
        f"SELECT COUNT(*) FROM bom_amendment_request_v2 WHERE {where}", *params,
    )
    rows = await conn.fetch(
        f"""
        SELECT * FROM bom_amendment_request_v2
        WHERE  {where}
        ORDER  BY created_at DESC, request_id DESC
        LIMIT  ${idx} OFFSET ${idx + 1}
        """,
        *params, page_size, offset,
    )
    return {
        "results": [_serialize(r) for r in rows],
        "pagination": {
            "page": page, "page_size": page_size, "total": total or 0,
            "total_pages": ((total or 0) + page_size - 1) // page_size if total else 0,
        },
    }


# ---------------------------------------------------------------------------
# Approve (advances status; on final approval runs the apply step)
# ---------------------------------------------------------------------------

class _AmendmentApplyError(Exception):
    """Raised by approve_amendment when the apply-step returns an error
    dict. Forces the surrounding asyncpg transaction (owned by the
    router) to roll back the status flip + any partial side-effects.
    Carries the apply-result dict so the router/upper layer can surface
    it after the rollback. See C1 in B11 review.
    """
    def __init__(self, apply_result: dict):
        super().__init__(apply_result.get("message") or "amendment apply failed")
        self.apply_result = apply_result


async def approve_amendment(conn, *, request_id: int,
                            checker_user_id: int,
                            note: str | None = None) -> dict:
    """Advance the amendment by one approval step. If the request_type
    requires only one checker, the first approval goes pending_review ->
    approved and the apply step runs. If the request type requires two
    checkers, the first approval advances pending_review -> pending_final;
    the second approval advances pending_final -> approved -> applied.

    All side effects (status change + apply) run in the caller's txn.
    If the apply step fails this RAISES _AmendmentApplyError so the
    outer txn rolls back the status flip; the amendment stays at the
    pre-approve status so the operator can retry. See C1.

    The `note` arg (H5) is persisted into checker1_note on the first
    approval and into checker2_note on the second approval. NULL/empty
    note skips the column update.
    """
    row = await conn.fetchrow(
        "SELECT * FROM bom_amendment_request_v2 WHERE request_id=$1 FOR UPDATE",
        request_id,
    )
    if row is None:
        return {"error": "amendment_not_found"}
    if row["status"] in TERMINAL_STATUSES:
        return {
            "error":  "amendment_terminal",
            "status": row["status"],
            "message": f"Cannot approve an amendment in '{row['status']}'.",
        }
    # H1: a row with maker_user_id IS NULL cannot enforce the
    # self-approval ban (and shouldn't exist after create_amendment
    # rejects None makers), so refuse rather than silently approve.
    if row["maker_user_id"] is None:
        return {
            "error": "maker_unknown",
            "message": (
                "Amendment has no recorded maker; refusing to approve "
                "until the maker is set (data repair required)."
            ),
        }
    if row["maker_user_id"] == checker_user_id:
        return {
            "error": "self_approval_forbidden",
            "message": "The maker of a request cannot approve it.",
        }

    needs_two = row["request_type"] in TWO_CHECKER_TYPES
    # The note column we write into depends on which slot we're
    # advancing. Compute it up-front so the UPDATE can be parameterised
    # uniformly. We use NULL-from-blank to preserve the no-note case.
    note_clean = (note or "").strip() or None
    if row["status"] == STATUS_PENDING_REVIEW:
        if row["checker1_user_id"] is not None and row["checker1_user_id"] != checker_user_id:
            # Already had a different first checker - shouldn't happen
            # if the maker UI prevents it, but defensive.
            return {
                "error": "duplicate_first_checker",
                "message": "This request already has a first checker recorded.",
            }
        new_status = STATUS_PENDING_FINAL if needs_two else STATUS_APPROVED
        update_sql = """
            UPDATE bom_amendment_request_v2
               SET status            = $2,
                   checker1_user_id  = COALESCE(checker1_user_id, $3),
                   checker1_note     = COALESCE($4, checker1_note),
                   decided_at        = CASE WHEN $2 = 'approved' THEN NOW() ELSE decided_at END
             WHERE request_id        = $1
            RETURNING *
        """
    elif row["status"] == STATUS_PENDING_FINAL:
        if not needs_two:
            return {
                "error": "invalid_state",
                "message": "Single-checker request shouldn't be in pending_final.",
            }
        if row["checker1_user_id"] == checker_user_id:
            return {
                "error": "duplicate_checker",
                "message": "Final approval must come from a different user than checker 1.",
            }
        new_status = STATUS_APPROVED
        update_sql = """
            UPDATE bom_amendment_request_v2
               SET status            = $2,
                   checker2_user_id  = COALESCE(checker2_user_id, $3),
                   checker2_note     = COALESCE($4, checker2_note),
                   decided_at        = NOW()
             WHERE request_id        = $1
            RETURNING *
        """
    else:
        return {
            "error":  "invalid_state",
            "status": row["status"],
            "message": f"Cannot approve from status '{row['status']}'.",
        }

    updated = await conn.fetchrow(
        update_sql, request_id, new_status, checker_user_id, note_clean,
    )
    result = {"updated": True, "amendment": _serialize(updated)}

    # If final-approved, run the apply step in the same txn.
    if updated["status"] == STATUS_APPROVED:
        apply_result = await _apply_amendment(conn, dict(updated))
        if apply_result.get("error"):
            # C1: raise so the surrounding asyncpg txn (router-owned)
            # rolls back the status flip and any partial side-effects.
            # The router/caller catches _AmendmentApplyError and surfaces
            # apply_result to the client.
            raise _AmendmentApplyError(apply_result)
        applied = await conn.fetchrow(
            """
            UPDATE bom_amendment_request_v2
               SET status     = $2,
                   applied_at = NOW(),
                   applied_version = $3
             WHERE request_id = $1
            RETURNING *
            """,
            request_id, STATUS_APPLIED, apply_result.get("applied_version"),
        )
        result["amendment"] = _serialize(applied)
        result["apply_result"] = apply_result

    return result


# ---------------------------------------------------------------------------
# Reject + Withdraw
# ---------------------------------------------------------------------------

async def reject_amendment(conn, *, request_id: int,
                           checker_user_id: int,
                           rejection_reason: str) -> dict:
    if not rejection_reason or not rejection_reason.strip():
        return {"error": "missing_rejection_reason"}
    row = await conn.fetchrow(
        "SELECT status, maker_user_id FROM bom_amendment_request_v2 "
        "WHERE  request_id=$1 FOR UPDATE",
        request_id,
    )
    if row is None:
        return {"error": "amendment_not_found"}
    if row["status"] in TERMINAL_STATUSES:
        return {"error": "amendment_terminal", "status": row["status"]}
    # H1: refuse to act on an orphaned-maker row (mirrors approve).
    if row["maker_user_id"] is None:
        return {
            "error": "maker_unknown",
            "message": (
                "Amendment has no recorded maker; refusing to reject "
                "until the maker is set (data repair required)."
            ),
        }
    if row["maker_user_id"] == checker_user_id:
        return {"error": "self_approval_forbidden"}
    updated = await conn.fetchrow(
        """
        UPDATE bom_amendment_request_v2
           SET status           = $2,
               rejection_reason = $3,
               decided_at       = NOW()
         WHERE request_id       = $1
        RETURNING *
        """,
        request_id, STATUS_REJECTED, rejection_reason.strip(),
    )
    return {"rejected": True, "amendment": _serialize(updated)}


async def withdraw_amendment(conn, *, request_id: int,
                             maker_user_id: int) -> dict:
    row = await conn.fetchrow(
        "SELECT status, maker_user_id FROM bom_amendment_request_v2 "
        "WHERE  request_id=$1 FOR UPDATE",
        request_id,
    )
    if row is None:
        return {"error": "amendment_not_found"}
    if row["status"] not in (STATUS_DRAFT, STATUS_PENDING_REVIEW):
        return {
            "error": "withdraw_not_allowed",
            "status": row["status"],
            "message": "Withdraw only allowed before any checker advances the request.",
        }
    if row["maker_user_id"] != maker_user_id:
        return {"error": "not_maker", "message": "Only the maker can withdraw their own request."}
    updated = await conn.fetchrow(
        """
        UPDATE bom_amendment_request_v2
           SET status     = $2,
               decided_at = NOW()
         WHERE request_id = $1
        RETURNING *
        """,
        request_id, STATUS_WITHDRAWN,
    )
    return {"withdrawn": True, "amendment": _serialize(updated)}


# ---------------------------------------------------------------------------
# Apply (per-type side effects)
# ---------------------------------------------------------------------------

async def _apply_amendment(conn, amendment: dict) -> dict:
    """Dispatch the side effects matching `amendment['request_type']`.
    Returns a dict that includes `applied_version` (for BOM amends) or
    error info. Caller is responsible for flipping status to 'applied'.
    """
    rt = amendment["request_type"]
    payload = amendment["payload_jsonb"] or {}
    if isinstance(payload, str):
        payload = json.loads(payload)

    if rt == 'one_off_material_add':
        return await _apply_one_off_material_add(conn, amendment, payload)
    if rt == 'one_off_material_remove':
        return await _apply_one_off_material_remove(conn, amendment, payload)
    if rt in ('permanent_bom_add', 'permanent_bom_remove', 'permanent_bom_qty_change'):
        return await _apply_permanent_bom(conn, amendment, payload, rt)
    if rt == 'unbalanced_close_override':
        # No side effect: the request_id itself is the audit token that
        # the /complete endpoint (B5) reads.
        return {"applied": True, "noop_reason": "consumed_at_complete_time"}
    if rt == 'ega_override':
        # No side effect at apply time; the JC's /accounting/byproducts
        # save will look up an approved override before B6's EGA gate.
        # (B11 -> B6 follow-up; today the JC simply records the EGA
        # and downstream gates accept it.)
        return {"applied": True, "noop_reason": "consumed_at_byproducts_save"}
    if rt == 'plan_delete':
        return await _apply_plan_delete(conn, payload)
    if rt == 'plan_field_change':
        return await _apply_plan_field_change(conn, payload)
    if rt == 'stop_process':
        # stop_process is executed by the /stop endpoint; this row is the
        # audit token. No side effect here.
        return {"applied": True, "noop_reason": "consumed_at_stop_time"}
    if rt == 'force_unlock':
        # Same pattern - the /force-unlock endpoint reads this row.
        return {"applied": True, "noop_reason": "consumed_at_force_unlock_time"}
    if rt == 'uom_correction':
        return await _apply_uom_correction(conn, payload)
    if rt == 'ncr_disposition':
        # NCR side effects are handled by the QC module; record-only here.
        return {"applied": True, "noop_reason": "delegated_to_qc"}
    return {
        "error": "unknown_request_type",
        "request_type": rt,
        "message": "No apply branch wired for this request_type.",
    }


async def _apply_one_off_material_add(conn, amendment: dict, payload: dict) -> dict:
    job_card_id = amendment.get("job_card_id")
    if job_card_id is None:
        return {"error": "missing_job_card_id"}
    exception = await conn.fetchrow(
        """
        INSERT INTO jc_material_exception_v2 (
            exception_id, job_card_id, request_id,
            material_sku_name, qty_kg, exception_type, reason, status
        ) VALUES ($1, $2, $3, $4, $5, 'one_off_add', $6, 'applied')
        RETURNING exception_id
        """,
        new_short_time_id(),
        job_card_id, amendment["request_id"],
        payload.get("material_sku_name"),
        payload.get("qty_kg"),
        amendment.get("reason"),
    )
    return {
        "applied": True,
        "exception_id": exception["exception_id"],
        "applied_version": None,
    }


async def _apply_one_off_material_remove(conn, amendment: dict, payload: dict) -> dict:
    job_card_id = amendment.get("job_card_id")
    if job_card_id is None:
        return {"error": "missing_job_card_id"}
    exception = await conn.fetchrow(
        """
        INSERT INTO jc_material_exception_v2 (
            exception_id, job_card_id, request_id,
            material_sku_name, exception_type, reason, status
        ) VALUES ($1, $2, $3, $4, 'one_off_remove', $5, 'applied')
        RETURNING exception_id
        """,
        new_short_time_id(),
        job_card_id, amendment["request_id"],
        payload.get("material_sku_name"),
        amendment.get("reason"),
    )
    return {
        "applied": True,
        "exception_id": exception["exception_id"],
        "applied_version": None,
    }


async def _apply_permanent_bom(conn, amendment: dict,
                                payload: dict, request_type: str) -> dict:
    """BOM amendment - bump bom_header.version, copy lines with the
    change applied. Mark the previous version's row inactive
    (is_active=FALSE) in the same txn so the uq_bom_header_active_fg
    partial UNIQUE (migration 032) holds; future plan_line
    materialisations pick up the new one. Existing in-flight JCs keep
    the version they were created against (job_card_v2 snapshots
    bom_id at creation time).

    Concurrency: acquires a pg_advisory_xact_lock keyed on
    hashtext(fg_sku_name) BEFORE reading current.version, so two
    concurrent permanent-BOM amendments on the same FG serialise
    instead of racing to insert duplicate (fg_sku_name, version) rows
    (C3). The advisory lock is held for the entire body of this
    function via _xact (auto-released at COMMIT/ROLLBACK), which also
    closes the H6 bom_line-copy race window. uq_bom_header_fg_version
    is the defense-in-depth backstop.

    All of the preserved bom_header columns (customer_name, entity,
    effective_from/to, item_group, notes) are carried forward to the
    new version (C2(b)) - without this a routine BOM amendment would
    silently strip out the customer scoping and effectivity dating.
    """
    bom_id = amendment.get("bom_id")
    if bom_id is None:
        return {"error": "missing_bom_id"}

    # Read fg_sku_name first (with a lock on the row) so the advisory
    # lock keys are consistent across racing transactions. Using the
    # one-arg pg_advisory_xact_lock(bigint) form with hashtext()
    # bucketed by fg_sku_name.
    current = await conn.fetchrow(
        """
        SELECT bom_id, fg_sku_name, customer_name, pack_size_kg, version,
               is_active, effective_from, effective_to, item_group,
               entity, notes
        FROM   bom_header
        WHERE  bom_id=$1
        FOR UPDATE
        """,
        bom_id,
    )
    if current is None:
        return {"error": "bom_not_found", "bom_id": bom_id}

    # C3 / H6: serialise concurrent amendments per fg_sku_name. The
    # lock auto-releases at txn end so both COMMIT and ROLLBACK are
    # safe; until then no other amendment on the same FG can proceed
    # past this point, which protects both the version bump AND the
    # bom_line INSERT-SELECT below.
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        current["fg_sku_name"],
    )

    # Re-read version after the lock in case a racer committed between
    # our SELECT FOR UPDATE and the lock acquisition (FOR UPDATE on
    # this bom_id locks this row only; another row for the same FG may
    # have been inserted by a parallel amend that ran first).
    current_version = await conn.fetchval(
        "SELECT COALESCE(MAX(version), 0) FROM bom_header WHERE fg_sku_name=$1",
        current["fg_sku_name"],
    )
    new_version = (current_version or 0) + 1

    # C2(a): flip the existing active row(s) for this FG to is_active=FALSE
    # so the uq_bom_header_active_fg partial UNIQUE permits the new row.
    await conn.execute(
        "UPDATE bom_header SET is_active=FALSE "
        "WHERE  fg_sku_name=$1 AND is_active=TRUE",
        current["fg_sku_name"],
    )

    # C2(b): preserve customer_name / entity / effective_from / effective_to
    # / item_group / notes from the previous version. is_active=TRUE on
    # the new row.
    new_bom = await conn.fetchrow(
        """
        INSERT INTO bom_header (
            fg_sku_name, customer_name, pack_size_kg, version,
            is_active, effective_from, effective_to, item_group,
            entity, notes
        )
        VALUES ($1, $2, $3, $4, TRUE, $5, $6, $7, $8, $9)
        RETURNING bom_id
        """,
        current["fg_sku_name"], current["customer_name"],
        current["pack_size_kg"], new_version,
        current["effective_from"], current["effective_to"],
        current["item_group"], current["entity"], current["notes"],
    )
    new_bom_id = new_bom["bom_id"]

    # Copy lines from the previous version (locked above; the advisory
    # lock prevents another amendment from inserting/deleting lines on
    # the source bom_id while we copy - H6).
    await conn.execute(
        """
        INSERT INTO bom_line (
            bom_id, line_number, material_sku_name, item_type,
            quantity_per_unit, uom, loss_pct, godown,
            can_use_offgrade, offgrade_max_pct
        )
        SELECT $2, line_number, material_sku_name, item_type,
               quantity_per_unit, uom, loss_pct, godown,
               can_use_offgrade, offgrade_max_pct
        FROM   bom_line
        WHERE  bom_id = $1
        """,
        bom_id, new_bom_id,
    )

    if request_type == 'permanent_bom_add':
        await conn.execute(
            """
            INSERT INTO bom_line (
                bom_id, line_number, material_sku_name, item_type,
                quantity_per_unit, uom, loss_pct
            )
            VALUES (
                $1,
                (SELECT COALESCE(MAX(line_number), 0) + 1 FROM bom_line WHERE bom_id=$1),
                $2, $3, $4, $5, COALESCE($6, 0)
            )
            """,
            new_bom_id,
            payload.get("material_sku_name"),
            payload.get("item_type"),
            payload.get("qty_per_unit"),
            payload.get("uom"),
            payload.get("loss_pct"),
        )
    elif request_type == 'permanent_bom_remove':
        await conn.execute(
            "DELETE FROM bom_line WHERE bom_id=$1 "
            "  AND line_number = (SELECT line_number FROM bom_line WHERE bom_line_id=$2)",
            new_bom_id, payload.get("bom_line_id"),
        )
    elif request_type == 'permanent_bom_qty_change':
        await conn.execute(
            "UPDATE bom_line SET quantity_per_unit=$3 "
            "WHERE  bom_id=$1 "
            "  AND  line_number = (SELECT line_number FROM bom_line WHERE bom_line_id=$2)",
            new_bom_id, payload.get("bom_line_id"), payload.get("new_qty"),
        )

    return {
        "applied": True,
        "new_bom_id": new_bom_id,
        "applied_version": new_version,
    }


async def _apply_plan_delete(conn, payload: dict) -> dict:
    """Cancel a production plan. H3:
      - SELECT ... FOR UPDATE locks the plan row for the txn so a
        racing approve can't flip status under us.
      - Pre-checks for active child JCs (status NOT IN closed terminal
        set) and refuses the cancel rather than orphaning live work.
      - Inspects the UPDATE result and treats 0-rows-affected as an
        error (silent no-op was masking already-cancelled/executed
        plans as successful applies).
    """
    plan_id = payload.get("plan_id")
    if plan_id is None:
        return {"error": "missing_plan_id"}

    # H3(a): row lock so concurrent approves serialise on this plan.
    plan = await conn.fetchrow(
        "SELECT plan_id, status FROM production_plan_v2 "
        "WHERE  plan_id=$1 FOR UPDATE",
        plan_id,
    )
    if plan is None:
        return {"error": "plan_not_found", "plan_id": plan_id}
    if plan["status"] in ('cancelled', 'executed'):
        return {
            "error": "plan_not_cancellable",
            "plan_id": plan_id,
            "status": plan["status"],
            "message": (
                f"plan is in terminal status '{plan['status']}'; cannot cancel."
            ),
        }

    # H3(b): refuse if any child JC is still alive.
    active_jc = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM job_card_v2
            WHERE  plan_id = $1
              AND  status NOT IN ('completed', 'cancelled', 'closed')
        )
        """,
        plan_id,
    )
    if active_jc:
        return {
            "error": "plan_has_active_job_cards",
            "plan_id": plan_id,
            "message": (
                "Cancel refused: this plan has at least one active job card. "
                "Close or cancel the JCs first, then re-approve."
            ),
        }

    # H3(c): inspect the UPDATE rowcount. asyncpg returns 'UPDATE N' as
    # the command tag; 0 rows means a racer beat us between the
    # SELECT FOR UPDATE and the UPDATE (shouldn't happen with the lock
    # held, but defensive).
    result = await conn.execute(
        "UPDATE production_plan_v2 SET status='cancelled' "
        "WHERE  plan_id=$1 AND status NOT IN ('cancelled','executed')",
        plan_id,
    )
    affected = 0
    try:
        affected = int(result.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        affected = 0
    if affected == 0:
        return {
            "error": "plan_cancel_noop",
            "plan_id": plan_id,
            "message": (
                "UPDATE affected 0 rows; another transaction may have "
                "changed the plan's status. Retry."
            ),
        }
    return {"applied": True, "applied_version": None,
            "update_result": result}


async def _apply_plan_field_change(conn, payload: dict) -> dict:
    """Apply an admin-approved plan_field_change amendment.

    Sequence (single txn — caller already opened one in approve_amendment):
      1. SELECT ... FOR UPDATE the plan row. Refuse if status is terminal
         (cancelled / executed).
      2. UPDATE production_plan_v2 with the plan-level fields (when any).
      3. For each line in line_changes: UPDATE production_plan_line_v2.
         Then cascade to child JCs whose status is in
         _PLAN_CASCADE_JC_STATUSES — push planned_qty_kg /
         planned_qty_units (if changed) onto job_card_v2; push
         deadline_date too. JCs past this status set are NOT touched —
         operator must handle them manually because variance / accounting
         has begun.

    Returns
      {
        "applied":         True,
        "applied_version": None,            # plans don't carry versions
        "plan_id":         <int>,
        "plan_updated":    <bool>,
        "lines_updated":   <int>,
        "jcs_cascaded":    <int>,
        "jcs_skipped":     <int>,            # past the cascade-status window
      }
    """
    plan_id = payload.get("plan_id")
    if plan_id is None:
        return {"error": "missing_plan_id"}

    plan = await conn.fetchrow(
        "SELECT plan_id, status FROM production_plan_v2 "
        "WHERE  plan_id=$1 FOR UPDATE",
        plan_id,
    )
    if plan is None:
        return {"error": "plan_not_found", "plan_id": plan_id}
    if plan["status"] in ('cancelled', 'executed'):
        return {
            "error":  "plan_not_editable",
            "plan_id": plan_id,
            "status":  plan["status"],
            "message": (
                f"plan is in terminal status '{plan['status']}'; "
                "field changes cannot be applied."
            ),
        }

    plan_fields  = payload.get("plan_fields")  or {}
    line_changes = payload.get("line_changes") or []

    plan_updated = False
    if plan_fields:
        sets: list[str] = []
        vals: list = []
        for k, v in plan_fields.items():
            if k in _PLAN_FIELD_KEYS:
                sets.append(f"{k}=${len(vals)+1}")
                vals.append(v)
        if sets:
            vals.append(plan_id)
            await conn.execute(
                f"UPDATE production_plan_v2 SET {', '.join(sets)} "
                f"WHERE plan_id=${len(vals)}",
                *vals,
            )
            plan_updated = True

    lines_updated = 0
    jcs_cascaded  = 0
    jcs_skipped   = 0
    for lc in line_changes:
        plan_line_id = lc.get("plan_line_id")
        fields = lc.get("fields") or {}
        if plan_line_id is None or not fields:
            continue
        # Apply on production_plan_line_v2 — column allow-list already
        # enforced in _validate_payload.
        sets: list[str] = []
        vals: list = []
        for k, v in fields.items():
            if k in _PLAN_LINE_FIELD_KEYS:
                sets.append(f"{k}=${len(vals)+1}")
                vals.append(v)
        if not sets:
            continue
        vals.append(plan_line_id)
        await conn.execute(
            f"UPDATE production_plan_line_v2 SET {', '.join(sets)} "
            f"WHERE plan_line_id=${len(vals)}",
            *vals,
        )
        lines_updated += 1

        # Cascade — only push fields that exist on job_card_v2 too
        # (planned_qty_kg, planned_qty_units, deadline_date), and only
        # to JCs whose status is still in the safe window.
        jc_fields_map = {
            'planned_qty_kg':    'planned_qty_kg',
            'planned_qty_units': 'planned_qty_units',
            'deadline_date':     'deadline_date',
        }
        jc_sets: list[str] = []
        jc_vals: list = []
        for k, col in jc_fields_map.items():
            if k in fields:
                jc_sets.append(f"{col}=${len(jc_vals)+1}")
                jc_vals.append(fields[k])
        if jc_sets:
            jc_vals.append(plan_line_id)
            # Count what'll be skipped first, for the response audit.
            skip_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM job_card_v2
                WHERE  plan_line_id=$1
                  AND  deleted_at IS NULL
                  AND  status NOT IN ('locked','unlocked','assigned')
                """,
                plan_line_id,
            ) or 0
            jcs_skipped += int(skip_count)
            ph = ', '.join(f'${len(jc_vals)+1+i}' for i in range(len(_PLAN_CASCADE_JC_STATUSES)))
            cascade_result = await conn.execute(
                f"""
                UPDATE job_card_v2 SET {', '.join(jc_sets)}
                WHERE  plan_line_id=${len(jc_vals)}
                  AND  deleted_at IS NULL
                  AND  status IN ({ph})
                """,
                *jc_vals, *_PLAN_CASCADE_JC_STATUSES,
            )
            try:
                jcs_cascaded += int(cascade_result.rsplit(" ", 1)[-1])
            except (ValueError, AttributeError):
                pass

    return {
        "applied":          True,
        "applied_version":  None,
        "plan_id":          plan_id,
        "plan_updated":     plan_updated,
        "lines_updated":    lines_updated,
        "jcs_cascaded":     jcs_cascaded,
        "jcs_skipped":      jcs_skipped,
    }


async def _apply_uom_correction(conn, payload: dict) -> dict:
    """C4: all_sku.uom is NUMERIC(15,3) per app/db/schema.sql:54 - it's
    the per-unit-kg multiplier, NOT a TEXT enum. So writing
    payload['new_uom_kg'] (a numeric) into all_sku.uom is correct;
    the payload key already reads cleanly as "new per-kg multiplier".

    Confirms 0-rows-affected as an error so an unknown sku_id surfaces
    instead of silently passing.
    """
    sku_id = payload.get("sku_id")
    new_uom_kg = payload.get("new_uom_kg")
    if sku_id is None or new_uom_kg is None:
        return {"error": "missing_uom_correction_fields"}
    result = await conn.execute(
        "UPDATE all_sku SET uom=$2 WHERE sku_id=$1",
        sku_id, new_uom_kg,
    )
    affected = 0
    try:
        affected = int(result.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        affected = 0
    if affected == 0:
        return {
            "error": "sku_not_found",
            "sku_id": sku_id,
            "message": "No all_sku row matched sku_id; uom not updated.",
        }
    return {"applied": True, "applied_version": None}
