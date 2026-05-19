"""NCR lifecycle service — raise / list / detail / disposition / CAPA / verify
                          / cancel / reopen / audit / dual-approve.

State machine (per spec §1):

    raised ──disposition──► dispositioned ──capa──► capa_pending ──capa(round n)──► capa_submitted
       │                          │                                                     │
       │                          ▼                                                     │
       │                   (no CAPA needed)                                              │
       │                          │                                                     ▼
       │                          ▼                                              verify(effective)──► closed
       │                       closed                                              verify(!effective)──► capa_failed_verification ──capa(round n+1)──► capa_submitted
       │
       └──────── cancel from any non-terminal ──────► cancelled
"""

from __future__ import annotations

import json
import logging
from datetime import date as _date, datetime, timezone
from typing import Any

import asyncpg

from core.middleware.request_context import AuthError
from modules.ncr.services import event_log
from modules.ncr.services.mint import mint_ncr_no

logger = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────────────────


_ALLOW_DISPOSITION_FROM = frozenset({"raised"})
_ALLOW_DISPOSITION_EDIT_FROM = frozenset({"dispositioned"})
_ALLOW_CAPA_FROM = frozenset({
    "dispositioned", "capa_pending", "capa_failed_verification",
})
_ALLOW_VERIFY_FROM = frozenset({"capa_submitted"})
_ALLOW_CANCEL_FROM = frozenset({
    "raised", "dispositioned", "capa_pending", "capa_submitted",
    "capa_failed_verification",
})
_ALLOW_REOPEN_FROM = frozenset({"closed", "cancelled"})
_ALLOW_DUAL_APPROVE_FROM = frozenset({
    "raised", "dispositioned", "capa_pending", "capa_submitted",
    "capa_failed_verification",
})


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        try:
            s = v.isoformat()
            return s.replace("+00:00", "Z") if "T" in s else s
        except (TypeError, ValueError):
            return str(v)
    return str(v)


def _coerce_date(v: Any) -> _date | None:
    if v is None or v == "":
        return None
    if isinstance(v, _date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        return _date.fromisoformat(str(v)[:10])
    except (ValueError, TypeError):
        return None


def _parse_jsonb(raw: Any) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def _infer_severity(parameter_severities: list[str]) -> str:
    """Default severity rule when severity isn't supplied: take the worst per-
    param severity; fall back to 'major' if none given (NCRs are non-trivial).

    WR-05: previous signature took rejected_qty too but never used it. Dropped.
    """
    rank = {"minor": 1, "major": 2, "critical": 3}
    worst = "major"
    for s in parameter_severities:
        if s and rank.get(s, 0) > rank.get(worst, 0):
            worst = s
    return worst


def _check_status(current: str, allowed: frozenset[str], action: str) -> None:
    if current not in allowed:
        raise AuthError(
            "invalid_status_transition",
            f"Cannot {action} an NCR in status {current!r}.",
            409,
            details={"current_status": current, "allowed": sorted(allowed)},
        )


async def _assert_active_user(conn, user_id: str | None, label: str) -> None:
    """HI-03 / HI-04: validate that a referenced user_id exists and is active."""
    if user_id is None:
        return
    try:
        row = await conn.fetchrow(
            "SELECT 1 FROM auth_user WHERE user_id::text = $1 AND is_active",
            str(user_id),
        )
    except Exception:
        row = None
    if row is None:
        raise AuthError(
            f"{label}_invalid",
            f"{label} user_id is unknown or inactive.",
            403,
        )


async def _assert_can_approve(conn, user_id: str, label: str) -> None:
    """HI-03 part 2: verify the named user (a) is active AND (b) actually
    holds `ncr.record.approve` — i.e. is an approver-class user. Without
    this, any active user could be designated a dual approver, defeating
    the spec's separation-of-duties intent.
    """
    if not user_id:
        raise AuthError(f"{label}_invalid", f"{label} user_id is required.", 403)
    row = await conn.fetchrow(
        """
        SELECT u.role_id,
               COALESCE(r.is_admin, FALSE) AS is_admin,
               u.is_active
          FROM auth_user u
          LEFT JOIN auth_role r ON u.role_id = r.role_id
         WHERE u.user_id::text = $1
        """,
        str(user_id),
    )
    if row is None or not row["is_active"]:
        raise AuthError(
            f"{label}_invalid",
            f"{label} user_id is unknown or inactive.",
            403,
        )
    from modules.auth.services.permission_service import check_permission
    ok = await check_permission(
        conn, row["role_id"], bool(row["is_admin"]),
        "ncr", "record", action="approve",
    )
    if not ok:
        raise AuthError(
            f"{label}_invalid",
            f"{label} does not hold the ncr.record.approve permission.",
            403,
        )


# ── 2.1 raise ────────────────────────────────────────────────────────────


async def raise_ncr(
    pool, *,
    inspection_id: str | None,
    transaction_no: str | None,
    line_number: int | None,
    sku_id: int | None,
    supplier_id: int,
    lot_number: str | None,
    rejected_qty: float,
    severity: str | None,
    summary: str,
    documented_date: str | None,
    parameter_details: list[dict],
    raised_via: str = "manual",
    actor_user_id: str,
) -> dict:
    final_severity = severity or _infer_severity(
        [p.get("severity") for p in (parameter_details or [])],
    )
    requires_dual = final_severity == "critical"
    documented = _coerce_date(documented_date) or datetime.now(timezone.utc).date()

    async with pool.acquire() as conn:
        async with conn.transaction():
            # HI-02: try to derive entity from the PO if transaction_no resolves.
            entity: str | None = None
            if transaction_no:
                row = await conn.fetchrow(
                    "SELECT entity FROM po_header WHERE transaction_no = $1",
                    transaction_no,
                )
                if row and row["entity"]:
                    entity = row["entity"]

            ncr_no = await mint_ncr_no(conn)
            now = datetime.now(timezone.utc)

            await conn.execute(
                """
                INSERT INTO ncr_record (
                    ncr_no, status, severity, raised_via,
                    inspection_id, transaction_no, line_number, sku_id,
                    supplier_id, lot_number, rejected_qty, summary,
                    documented_date, raised_at, raised_by, requires_dual_approval,
                    entity
                )
                VALUES (
                    $1, 'raised', $2, $3,
                    $4, $5, $6, $7,
                    $8, $9, $10, $11,
                    $12, $13, $14, $15,
                    $16
                )
                """,
                ncr_no, final_severity, raised_via,
                inspection_id, transaction_no, line_number, sku_id,
                supplier_id, lot_number, rejected_qty, summary,
                documented, now, actor_user_id, requires_dual,
                entity,
            )
            for p in parameter_details or []:
                await conn.execute(
                    """
                    INSERT INTO ncr_parameter_detail (
                        ncr_no, parameter_id, parameter_name,
                        observed_value_text, observed_value_num,
                        spec_min, spec_max, deviation_pct,
                        severity, notes
                    )
                    VALUES (
                        $1, $2, $3,
                        $4, NULL,
                        $5, $6, $7,
                        $8, NULL
                    )
                    """,
                    ncr_no, p.get("parameter_id"), None,
                    str(p.get("observed_value", "")),
                    p.get("spec_min"), p.get("spec_max"), p.get("deviation_pct"),
                    p.get("severity"),
                )

            await event_log.write(
                conn, ncr_no=ncr_no,
                event_type=(
                    event_log.EV_RAISED_VIA_OVERRIDE if raised_via == "qc_override"
                    else event_log.EV_RAISED
                ),
                occurred_by=actor_user_id,
                payload={
                    "raised_via": raised_via,
                    "severity": final_severity,
                    "rejected_qty": rejected_qty,
                    "supplier_id": supplier_id,
                    "entity": entity,
                    "parameter_count": len(parameter_details or []),
                },
            )

    return {
        "ncr_no": ncr_no,
        "status": "raised",
        "severity": final_severity,
        "raised_at": _iso(now),
        "raised_by": actor_user_id,
        "requires_dual_approval": requires_dual,
        "notifications_dispatched": 0,
    }


# ── 2.2 list ─────────────────────────────────────────────────────────────


async def list_ncrs(
    pool, *,
    status: str | None,
    severity: str | None,
    supplier_id: int | None,
    sku_id: int | None,
    disposition: str | None,
    from_date: str | None,
    to_date: str | None,
    page: int,
    page_size: int,
    entity_scope: list[str] | None = None,
) -> dict:
    """HI-02: `entity_scope=None` means unrestricted (admin); a list scopes
    rows. `entity IS NULL` rows are visible to admins only — non-admins see
    only rows whose entity is in their allowed list."""
    conds: list[str] = []
    params: list[Any] = []
    idx = 0

    def add(sql: str, *vals: Any) -> None:
        nonlocal idx
        out = sql
        for v in vals:
            idx += 1
            out = out.replace("${i}", f"${idx}", 1)
            params.append(v)
        conds.append(out)

    for k, v in (("status", status), ("severity", severity),
                 ("disposition", disposition)):
        if v:
            add(f"{k} = ${{i}}", v)
    if supplier_id is not None:
        add("supplier_id = ${i}", supplier_id)
    if sku_id is not None:
        add("sku_id = ${i}", sku_id)
    df = _coerce_date(from_date)
    dt = _coerce_date(to_date)
    if df:
        add("raised_at >= ${i}::date", df)
    if dt:
        add("raised_at <  (${i}::date + INTERVAL '1 day')", dt)
    if entity_scope is not None:
        # NEW-05: allow NULL-entity rows (walk-in / no-PO NCRs that the
        # backfill couldn't tag) to remain visible to non-admins. Pre-fix
        # there was no scoping at all; tightening to strict equality would
        # silently hide historical no-PO rows from QC inspectors and viewers.
        # Admins still see everything via entity_scope=None.
        if not entity_scope:
            # Empty list → user has no entity grants → see only NULL-entity
            # rows (legacy data). Effectively the most-restricted state.
            conds.append("entity IS NULL")
        else:
            idx += 1
            conds.append(f"(entity = ANY(${idx}::text[]) OR entity IS NULL)")
            params.append(list(entity_scope))

    where_sql = " AND ".join(conds) if conds else "TRUE"
    offset = (page - 1) * page_size

    async with pool.acquire() as conn:
        total = int(await conn.fetchval(
            f"SELECT COUNT(*) FROM ncr_record WHERE {where_sql}", *params,
        ) or 0)
        rows = await conn.fetch(
            f"""
            SELECT n.*,
                   (SELECT po_number FROM po_header WHERE transaction_no = n.transaction_no) AS po_number,
                   (SELECT COUNT(*) FROM ncr_supplier_action a WHERE a.ncr_no = n.ncr_no) AS capa_rounds_count
              FROM ncr_record n
             WHERE {where_sql}
             ORDER BY raised_at DESC NULLS LAST, ncr_no DESC
             LIMIT ${idx + 1} OFFSET ${idx + 2}
            """,
            *params, page_size, offset,
        )

    items = [_row_to_listitem(r) for r in rows]
    return {"total": total, "page": page, "page_size": page_size, "items": items}


def _row_to_listitem(row) -> dict:
    return {
        "ncr_no": row["ncr_no"],
        "status": row["status"],
        "severity": row["severity"],
        "inspection_id": row["inspection_id"],
        "transaction_no": row["transaction_no"],
        "po_number": row["po_number"] if "po_number" in row.keys() else None,
        "sku_id": row["sku_id"],
        "sku_name": row["sku_name"],
        "supplier_id": row["supplier_id"],
        "supplier_name": row["supplier_name"],
        "lot_number": row["lot_number"],
        "rejected_qty": float(row["rejected_qty"]) if row["rejected_qty"] is not None else 0.0,
        "disposition": row["disposition"],
        "raised_at": _iso(row["raised_at"]),
        "raised_by_name": row["raised_by"],
        "capa_rounds_count": int(row["capa_rounds_count"]) if "capa_rounds_count" in row.keys() else 0,
        "closure_tat_days": float(row["closure_tat_days"]) if row["closure_tat_days"] is not None else None,
        "closed_at": _iso(row["closed_at"]),
    }


# ── 2.3 detail ───────────────────────────────────────────────────────────


async def get_detail(pool, *, ncr_no: str,
                     entity_scope: list[str] | None = None) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT n.*,
                   (SELECT po_number FROM po_header WHERE transaction_no = n.transaction_no) AS po_number,
                   (SELECT COUNT(*) FROM ncr_supplier_action a WHERE a.ncr_no = n.ncr_no) AS capa_rounds_count
              FROM ncr_record n
             WHERE n.ncr_no = $1
            """,
            ncr_no,
        )
        if row is None:
            raise AuthError("ncr_not_found", "NCR not found", 404)
        # HI-02: entity-scope mismatch → 404 (don't leak existence cross-entity)
        if entity_scope is not None and row["entity"] and \
                row["entity"].lower() not in entity_scope:
            raise AuthError("ncr_not_found", "NCR not found", 404)

        params = await conn.fetch(
            "SELECT * FROM ncr_parameter_detail WHERE ncr_no = $1 ORDER BY detail_id",
            ncr_no,
        )
        actions = await conn.fetch(
            "SELECT * FROM ncr_supplier_action WHERE ncr_no = $1 ORDER BY round_no",
            ncr_no,
        )

    payload = _row_to_listitem(row)
    payload["summary"] = row["summary"]
    payload["documented_date"] = _iso(row["documented_date"])
    payload["parameter_details"] = [
        {
            "detail_id": p["detail_id"],
            "parameter_id": p["parameter_id"],
            "parameter_name": p["parameter_name"],
            "observed_value_text": p["observed_value_text"],
            "observed_value_num": float(p["observed_value_num"]) if p["observed_value_num"] is not None else None,
            "spec_min": float(p["spec_min"]) if p["spec_min"] is not None else None,
            "spec_max": float(p["spec_max"]) if p["spec_max"] is not None else None,
            "deviation_pct": float(p["deviation_pct"]) if p["deviation_pct"] is not None else None,
            "severity": p["severity"],
            "notes": p["notes"],
            "created_at": _iso(p["created_at"]),
        }
        for p in params
    ]
    payload["supplier_actions"] = [_action_row_to_dict(a) for a in actions]

    verified = next(
        (a for a in reversed(actions) if a["verified_at"] is not None),
        None,
    )
    payload["verification"] = _action_row_to_verification(verified) if verified else None
    payload["cancel_reason"] = row["cancel_reason"]
    payload["reopen_reason"] = row["reopen_reason"]
    payload["dual_approval_by_name"] = row["dual_approval_by"]
    return payload


async def _scope_check_ncr(conn, ncr_no: str,
                          entity_scope: list[str] | None) -> None:
    """Mutator-side scope check: 404 if NCR's entity isn't in scope. Caller
    must have already ensured the row exists (otherwise we'd 404 here too)."""
    if entity_scope is None:
        return
    row = await conn.fetchrow(
        "SELECT entity FROM ncr_record WHERE ncr_no = $1", ncr_no,
    )
    if row is None:
        raise AuthError("ncr_not_found", "NCR not found", 404)
    if row["entity"] and row["entity"].lower() not in entity_scope:
        raise AuthError("ncr_not_found", "NCR not found", 404)


def _action_row_to_dict(row) -> dict:
    return {
        "action_id": int(row["action_id"]),
        "round_no": int(row["round_no"]),
        "root_cause": row["root_cause"],
        "corrective_action": row["corrective_action"],
        "preventive_action": row["preventive_action"],
        "target_date": _iso(row["target_date"]),
        "responsible_person": row["responsible_person"],
        "evidence_s3_keys": list(row["evidence_s3_keys"] or []),
        "submitted_at": _iso(row["submitted_at"]),
        "submitted_via": row["submitted_via"],
        "verified_at": _iso(row["verified_at"]),
        "verified_by": row["verified_by"],
        "is_effective": row["is_effective"],
        "verification_notes": row["verification_notes"],
        "verification_evidence_s3_keys": list(row["verification_evidence_s3_keys"] or []),
    }


def _action_row_to_verification(row) -> dict:
    return {
        "action_id": int(row["action_id"]),
        "is_effective": bool(row["is_effective"]),
        "verification_notes": row["verification_notes"] or "",
        "verification_evidence_s3_keys": list(row["verification_evidence_s3_keys"] or []),
        "verified_at": _iso(row["verified_at"]),
        "verified_by": row["verified_by"],
        "verified_by_name": row["verified_by"],
    }


# ── 2.4 update header ────────────────────────────────────────────────────


async def update_header(
    pool, *, ncr_no: str, severity: str | None,
    summary: str | None, documented_date: str | None,
    actor_user_id: str, entity_scope: list[str] | None = None,
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _scope_check_ncr(conn, ncr_no, entity_scope)
            row = await conn.fetchrow(
                "SELECT status FROM ncr_record WHERE ncr_no = $1 FOR UPDATE", ncr_no,
            )
            if row is None:
                raise AuthError("ncr_not_found", "NCR not found", 404)
            _check_status(row["status"], frozenset({"raised"}), "edit header on")

            await conn.execute(
                """
                UPDATE ncr_record SET
                    severity        = COALESCE($2, severity),
                    summary         = COALESCE($3, summary),
                    documented_date = COALESCE($4, documented_date)
                 WHERE ncr_no = $1
                """,
                ncr_no, severity, summary, _coerce_date(documented_date),
            )
            await event_log.write(
                conn, ncr_no=ncr_no, event_type=event_log.EV_HEADER_UPDATED,
                occurred_by=actor_user_id,
                payload={k: v for k, v in {
                    "severity": severity, "summary": summary,
                    "documented_date": documented_date,
                }.items() if v is not None},
            )


# ── 2.5 / 2.6 disposition ────────────────────────────────────────────────


async def submit_disposition(
    pool, *, ncr_no: str, body: dict, actor_user_id: str,
    entity_scope: list[str] | None = None,
) -> dict:
    disposition = body["disposition"]
    if disposition == "return_to_vendor":
        if not body.get("rtv_vehicle_no") or not body.get("rtv_lr_no"):
            raise AuthError(
                "validation_error",
                "rtv_vehicle_no and rtv_lr_no are required for return_to_vendor.",
                400,
            )

    async with pool.acquire() as conn:
        async with conn.transaction():
            await _scope_check_ncr(conn, ncr_no, entity_scope)
            row = await conn.fetchrow(
                "SELECT status, severity, raised_by FROM ncr_record WHERE ncr_no = $1 FOR UPDATE",
                ncr_no,
            )
            if row is None:
                raise AuthError("ncr_not_found", "NCR not found", 404)
            _check_status(row["status"], _ALLOW_DISPOSITION_FROM, "disposition")

            sev = row["severity"]
            concurrence = body.get("concurrence_user_id")
            if sev in ("major", "critical") and not concurrence:
                raise AuthError(
                    "concurrence_required",
                    f"{sev}-severity NCRs require a concurrence_user_id (second-pair-of-eyes).",
                    403,
                )
            if concurrence:
                concurrence_str = str(concurrence)
                # HI-04 + WR-02: concurrer must be a real active user, must
                # differ from both the raiser AND the actor (dispositioner).
                await _assert_active_user(conn, concurrence_str, "concurrence")
                if concurrence_str == str(row["raised_by"]):
                    raise AuthError(
                        "concurrence_required",
                        "Concurrence user must differ from the raiser.",
                        403,
                    )
                if concurrence_str == str(actor_user_id):
                    raise AuthError(
                        "concurrence_required",
                        "Concurrence user must differ from the dispositioner (you).",
                        403,
                    )

            now = datetime.now(timezone.utc)
            await conn.execute(
                """
                UPDATE ncr_record SET
                    status               = 'dispositioned',
                    disposition          = $2,
                    disposition_qty      = $3,
                    financial_impact     = $4,
                    rationale            = $5,
                    rtv_vehicle_no       = $6,
                    rtv_lr_no            = $7,
                    concurrence_user_id  = $8,
                    disposition_at       = $9,
                    disposition_by       = $10
                 WHERE ncr_no = $1
                """,
                ncr_no, disposition, body["disposition_qty"],
                body.get("financial_impact"), body["rationale"],
                body.get("rtv_vehicle_no"), body.get("rtv_lr_no"),
                concurrence,
                now, actor_user_id,
            )
            next_step = (
                "capa_required"
                if (sev in ("major", "critical")
                    or disposition in ("reject", "rework", "return_to_vendor"))
                else "capa_optional"
            )
            await event_log.write(
                conn, ncr_no=ncr_no, event_type=event_log.EV_DISPOSITIONED,
                occurred_by=actor_user_id,
                payload={
                    "disposition": disposition,
                    "disposition_qty": body["disposition_qty"],
                    "next_step": next_step,
                },
            )
    return {
        "ncr_no": ncr_no,
        "disposition": disposition,
        "disposition_at": _iso(now),
        "next_step": next_step,
    }


async def edit_disposition(
    pool, *, ncr_no: str, body: dict, actor_user_id: str,
    entity_scope: list[str] | None = None,
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _scope_check_ncr(conn, ncr_no, entity_scope)
            row = await conn.fetchrow(
                """
                SELECT status,
                       (SELECT COUNT(*) FROM ncr_supplier_action WHERE ncr_no = $1) AS capa_count
                  FROM ncr_record WHERE ncr_no = $1 FOR UPDATE
                """,
                ncr_no,
            )
            if row is None:
                raise AuthError("ncr_not_found", "NCR not found", 404)
            _check_status(row["status"], _ALLOW_DISPOSITION_EDIT_FROM, "edit disposition on")
            if row["capa_count"] and int(row["capa_count"]) > 0:
                raise AuthError(
                    "invalid_status_transition",
                    "Cannot edit disposition once a CAPA round has been filed.",
                    409,
                )
            await conn.execute(
                """
                UPDATE ncr_record SET
                    disposition     = COALESCE($2, disposition),
                    disposition_qty = COALESCE($3, disposition_qty),
                    rationale       = COALESCE($4, rationale)
                 WHERE ncr_no = $1
                """,
                ncr_no, body.get("disposition"), body.get("disposition_qty"),
                body.get("rationale"),
            )
            await event_log.write(
                conn, ncr_no=ncr_no, event_type=event_log.EV_DISPOSITION_EDITED,
                occurred_by=actor_user_id,
                payload={
                    "edit_reason": body["edit_reason"],
                    **{k: v for k, v in {
                        "disposition": body.get("disposition"),
                        "disposition_qty": body.get("disposition_qty"),
                        "rationale": body.get("rationale"),
                    }.items() if v is not None},
                },
            )


# ── NEW: 2.5b dual-approve (CR-01) ───────────────────────────────────────


async def set_dual_approval(
    pool, *, ncr_no: str, approver_user_id: str, reason: str,
    actor_user_id: str, entity_scope: list[str] | None = None,
) -> dict:
    """CR-01: record dual approval on a critical NCR so it can be closed.

    The recorded approver must:
        • exist + be active in auth_user
        • differ from the actor (the approver vouches for the actor's work)
        • differ from the raiser, dispositioner, and verifier (separation
          of duties — the dual approver is a fourth actor)
    """
    if str(approver_user_id) == str(actor_user_id):
        raise AuthError(
            "dual_approver_invalid",
            "Dual approver must differ from the actor recording approval.",
            403,
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            await _scope_check_ncr(conn, ncr_no, entity_scope)
            await _assert_can_approve(conn, approver_user_id, "dual_approver")
            row = await conn.fetchrow(
                """
                SELECT status, raised_by, disposition_by, concurrence_user_id
                  FROM ncr_record WHERE ncr_no = $1 FOR UPDATE
                """,
                ncr_no,
            )
            if row is None:
                raise AuthError("ncr_not_found", "NCR not found", 404)
            _check_status(row["status"], _ALLOW_DUAL_APPROVE_FROM, "dual-approve")

            approver = str(approver_user_id)
            for who, label in (
                (row["raised_by"], "raiser"),
                (row["disposition_by"], "dispositioner"),
                (row["concurrence_user_id"], "concurrer"),
            ):
                if who and str(who) == approver:
                    raise AuthError(
                        "dual_approver_invalid",
                        f"Dual approver cannot also be the {label}.",
                        403,
                    )

            now = datetime.now(timezone.utc)
            await conn.execute(
                """
                UPDATE ncr_record SET
                    dual_approval_by = $2,
                    dual_approval_at = $3
                 WHERE ncr_no = $1
                """,
                ncr_no, approver, now,
            )
            await event_log.write(
                conn, ncr_no=ncr_no, event_type=event_log.EV_DUAL_APPROVED,
                occurred_by=actor_user_id,
                payload={"dual_approver_user_id": approver, "reason": reason},
            )
    return {
        "ncr_no": ncr_no,
        "dual_approval_by": approver,
        "dual_approval_at": _iso(now),
    }


# ── 2.7 / 2.8 / 2.9 CAPA ─────────────────────────────────────────────────


async def submit_capa(
    pool, *, ncr_no: str, body: dict, actor_user_id: str,
    entity_scope: list[str] | None = None,
) -> dict:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _scope_check_ncr(conn, ncr_no, entity_scope)
            row = await conn.fetchrow(
                "SELECT status FROM ncr_record WHERE ncr_no = $1 FOR UPDATE", ncr_no,
            )
            if row is None:
                raise AuthError("ncr_not_found", "NCR not found", 404)
            _check_status(row["status"], _ALLOW_CAPA_FROM, "file CAPA on")

            pending = await conn.fetchrow(
                """
                SELECT 1 FROM ncr_supplier_action
                 WHERE ncr_no = $1 AND verified_at IS NULL
                 LIMIT 1
                """,
                ncr_no,
            )
            if pending:
                raise AuthError(
                    "capa_already_pending_verification",
                    "Verify the previous CAPA round before filing a new one.",
                    409,
                )

            next_round = await conn.fetchval(
                "SELECT COALESCE(MAX(round_no), 0) + 1 FROM ncr_supplier_action WHERE ncr_no = $1",
                ncr_no,
            )
            target_d = _coerce_date(body["target_date"])
            if target_d is None:
                raise AuthError("validation_error", "target_date must be YYYY-MM-DD", 400)

            row2 = await conn.fetchrow(
                """
                INSERT INTO ncr_supplier_action (
                    ncr_no, round_no, root_cause, corrective_action,
                    preventive_action, target_date, responsible_person,
                    evidence_s3_keys, submitted_via, submitted_by
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING action_id, submitted_at
                """,
                ncr_no, int(next_round), body["root_cause"], body["corrective_action"],
                body.get("preventive_action"), target_d, body["responsible_person"],
                list(body.get("evidence_s3_keys") or []),
                body.get("submitted_via") or "purchase_relay", actor_user_id,
            )
            await conn.execute(
                "UPDATE ncr_record SET status = 'capa_submitted' WHERE ncr_no = $1",
                ncr_no,
            )
            await event_log.write(
                conn, ncr_no=ncr_no, event_type=event_log.EV_CAPA_SUBMITTED,
                occurred_by=actor_user_id,
                payload={"round_no": int(next_round), "action_id": int(row2["action_id"])},
            )

    return {
        "action_id": int(row2["action_id"]),
        "ncr_no": ncr_no,
        "round_no": int(next_round),
        "submitted_at": _iso(row2["submitted_at"]),
    }


# WR-04: sort the allowlist for deterministic SQL parameter ordering
_CAPA_EDITABLE_COLUMNS: tuple[str, ...] = (
    "corrective_action", "evidence_s3_keys", "preventive_action",
    "responsible_person", "root_cause", "target_date",
)


async def update_capa(
    pool, *, ncr_no: str, action_id: int, body: dict, actor_user_id: str,
    entity_scope: list[str] | None = None,
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _scope_check_ncr(conn, ncr_no, entity_scope)
            row = await conn.fetchrow(
                """
                SELECT verified_at FROM ncr_supplier_action
                 WHERE ncr_no = $1 AND action_id = $2 FOR UPDATE
                """,
                ncr_no, action_id,
            )
            if row is None:
                raise AuthError("capa_not_found", "CAPA round not found", 404)
            if row["verified_at"] is not None:
                raise AuthError(
                    "already_verified",
                    "This CAPA round has been verified and cannot be edited.",
                    409,
                )

            updates: list[str] = []
            params: list[Any] = []
            idx = 1
            for k in _CAPA_EDITABLE_COLUMNS:
                if k not in body:
                    continue
                v = body.get(k)
                if v is None:
                    continue
                col_value: Any = v
                if k == "target_date":
                    col_value = _coerce_date(v)
                    if col_value is None:
                        raise AuthError("validation_error", "target_date must be YYYY-MM-DD", 400)
                if k == "evidence_s3_keys":
                    col_value = list(v)
                updates.append(f'"{k}" = ${idx}')
                params.append(col_value)
                idx += 1

            if updates:
                params.extend([ncr_no, action_id])
                sql = (
                    f"UPDATE ncr_supplier_action SET {', '.join(updates)} "
                    f"WHERE ncr_no = ${idx} AND action_id = ${idx + 1}"
                )
                await conn.execute(sql, *params)

            await event_log.write(
                conn, ncr_no=ncr_no, event_type=event_log.EV_CAPA_UPDATED,
                occurred_by=actor_user_id,
                payload={
                    "action_id": action_id,
                    "edit_reason": body["edit_reason"],
                    "fields_changed": [k for k in _CAPA_EDITABLE_COLUMNS
                                       if body.get(k) is not None],
                },
            )


async def delete_capa(
    pool, *, ncr_no: str, action_id: int, actor_user_id: str,
    entity_scope: list[str] | None = None,
) -> None:
    """CR-02: post-delete state recompute walks the remaining rounds:
        • zero rounds                       → status='dispositioned'
        • latest remaining = ineffective    → status='capa_failed_verification'
        • latest remaining = unverified     → status='capa_submitted'
    Verified-effective rounds are not deletable (NCR is closed in that case
    and writes are blocked by the status check on `_ALLOW_CAPA_FROM`).
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _scope_check_ncr(conn, ncr_no, entity_scope)
            row = await conn.fetchrow(
                """
                SELECT verified_at, round_no FROM ncr_supplier_action
                 WHERE ncr_no = $1 AND action_id = $2 FOR UPDATE
                """,
                ncr_no, action_id,
            )
            if row is None:
                raise AuthError("capa_not_found", "CAPA round not found", 404)
            if row["verified_at"] is not None:
                raise AuthError(
                    "already_verified",
                    "This CAPA round has been verified and cannot be deleted.",
                    409,
                )
            await conn.execute(
                "DELETE FROM ncr_supplier_action WHERE ncr_no = $1 AND action_id = $2",
                ncr_no, action_id,
            )

            # Recompute status from the latest remaining round
            latest = await conn.fetchrow(
                """
                SELECT verified_at, is_effective
                  FROM ncr_supplier_action
                 WHERE ncr_no = $1
                 ORDER BY round_no DESC
                 LIMIT 1
                """,
                ncr_no,
            )
            if latest is None:
                new_status = "dispositioned"
            elif latest["verified_at"] is None:
                new_status = "capa_submitted"
            elif latest["is_effective"] is False:
                new_status = "capa_failed_verification"
            else:
                # Latest is verified-effective — NCR should be 'closed' already.
                # Don't touch it; this branch is unreachable for unverified-only
                # delete contracts but defensive.
                new_status = "closed"

            await conn.execute(
                "UPDATE ncr_record SET status = $2 WHERE ncr_no = $1",
                ncr_no, new_status,
            )
            await event_log.write(
                conn, ncr_no=ncr_no, event_type=event_log.EV_CAPA_DELETED,
                occurred_by=actor_user_id,
                payload={
                    "action_id": action_id,
                    "round_no": int(row["round_no"]),
                    "new_status": new_status,
                },
            )


# ── 2.10 verify ──────────────────────────────────────────────────────────


async def verify_capa(
    pool, *, ncr_no: str, body: dict, actor_user_id: str,
    entity_scope: list[str] | None = None,
) -> dict:
    """WR-06: dropped unused `actor_is_admin` param.
    HI-01: requires the action_id to be the LATEST round (no skipping).
    HI-05: verifier ≠ raiser, dispositioner, concurrer, or recorded dual
    approver — full separation-of-duties chain.
    """
    action_id = int(body["action_id"])
    is_effective = bool(body["is_effective"])

    async with pool.acquire() as conn:
        async with conn.transaction():
            await _scope_check_ncr(conn, ncr_no, entity_scope)
            ncr = await conn.fetchrow(
                """
                SELECT status, severity, raised_by, disposition_by,
                       concurrence_user_id, requires_dual_approval,
                       dual_approval_by, raised_at
                  FROM ncr_record WHERE ncr_no = $1 FOR UPDATE
                """,
                ncr_no,
            )
            if ncr is None:
                raise AuthError("ncr_not_found", "NCR not found", 404)

            # HI-01: only the latest unverified round may be verified.
            latest_round = await conn.fetchval(
                "SELECT MAX(round_no) FROM ncr_supplier_action WHERE ncr_no = $1",
                ncr_no,
            )
            target = await conn.fetchrow(
                """
                SELECT verified_at, round_no FROM ncr_supplier_action
                 WHERE ncr_no = $1 AND action_id = $2 FOR UPDATE
                """,
                ncr_no, action_id,
            )
            if target is None:
                raise AuthError("capa_not_found", "CAPA round not found", 404)
            if target["verified_at"] is not None:
                raise AuthError(
                    "already_verified",
                    "This CAPA round has been verified.",
                    409,
                )
            if int(target["round_no"]) != int(latest_round or 0):
                raise AuthError(
                    "invalid_status_transition",
                    "Only the latest CAPA round may be verified.",
                    409,
                    details={
                        "target_round": int(target["round_no"]),
                        "latest_round": int(latest_round or 0),
                    },
                )

            # HI-05: full SoD chain
            actor = str(actor_user_id)
            for who, label in (
                (ncr["raised_by"], "raiser"),
                (ncr["disposition_by"], "dispositioner"),
                (ncr["concurrence_user_id"], "concurrer"),
                (ncr["dual_approval_by"], "dual_approver"),
            ):
                if who and str(who) == actor:
                    raise AuthError(
                        "permission_denied",
                        f"Verifier cannot be the same person as the {label}.",
                        403,
                        details={"role": label},
                    )

            if (
                is_effective
                and ncr["requires_dual_approval"]
                and not ncr["dual_approval_by"]
            ):
                raise AuthError(
                    "dual_approval_required",
                    "Critical NCRs require a recorded dual approver before closure. "
                    "Use POST /api/v1/ncr/{ncr_no}/dual-approve first.",
                    403,
                )

            now = datetime.now(timezone.utc)
            await conn.execute(
                """
                UPDATE ncr_supplier_action SET
                    verified_at                   = $3,
                    verified_by                   = $4,
                    is_effective                  = $5,
                    verification_notes            = $6,
                    verification_evidence_s3_keys = $7
                 WHERE ncr_no = $1 AND action_id = $2
                """,
                ncr_no, action_id, now, actor_user_id, is_effective,
                body["verification_notes"],
                list(body.get("verification_evidence_s3_keys") or []),
            )

            if is_effective:
                tat = (now - ncr["raised_at"]).total_seconds() / 86400
                await conn.execute(
                    """
                    UPDATE ncr_record SET
                        status           = 'closed',
                        closed_at        = $2,
                        closure_tat_days = $3
                     WHERE ncr_no = $1
                    """,
                    ncr_no, now, round(tat, 2),
                )
                await event_log.write(
                    conn, ncr_no=ncr_no, event_type=event_log.EV_VERIFIED_EFFECTIVE,
                    occurred_by=actor_user_id,
                    payload={"action_id": action_id, "closure_tat_days": round(tat, 2)},
                )
                await event_log.write(
                    conn, ncr_no=ncr_no, event_type=event_log.EV_CLOSED,
                    occurred_by=actor_user_id,
                    payload={"action_id": action_id},
                )
                new_status = "closed"
                tat_out: float | None = round(tat, 2)
            else:
                await conn.execute(
                    "UPDATE ncr_record SET status = 'capa_failed_verification' WHERE ncr_no = $1",
                    ncr_no,
                )
                await event_log.write(
                    conn, ncr_no=ncr_no, event_type=event_log.EV_VERIFIED_INEFFECTIVE,
                    occurred_by=actor_user_id,
                    payload={"action_id": action_id},
                )
                new_status = "capa_failed_verification"
                tat_out = None

    return {
        "ncr_no": ncr_no,
        "action_id": action_id,
        "is_effective": is_effective,
        "verified_at": _iso(now),
        "new_status": new_status,
        "closure_tat_days": tat_out,
    }


# ── 2.11 / 2.12 cancel + reopen ──────────────────────────────────────────


async def cancel_ncr(
    pool, *, ncr_no: str, reason: str, actor_user_id: str,
    cascade_event: str | None = None,
    entity_scope: list[str] | None = None,
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _scope_check_ncr(conn, ncr_no, entity_scope)
            row = await conn.fetchrow(
                "SELECT status FROM ncr_record WHERE ncr_no = $1 FOR UPDATE", ncr_no,
            )
            if row is None:
                raise AuthError("ncr_not_found", "NCR not found", 404)
            _check_status(row["status"], _ALLOW_CANCEL_FROM, "cancel")
            await conn.execute(
                """
                UPDATE ncr_record SET
                    status        = 'cancelled',
                    cancel_reason = $2,
                    cancelled_at  = NOW(),
                    cancelled_by  = $3
                 WHERE ncr_no = $1
                """,
                ncr_no, reason, actor_user_id,
            )
            await event_log.write(
                conn, ncr_no=ncr_no,
                event_type=cascade_event or event_log.EV_CANCELLED,
                occurred_by=actor_user_id,
                payload={"reason": reason},
            )


async def reopen_ncr(
    pool, *, ncr_no: str, reason: str, dual_approver_user_id: str,
    actor_user_id: str, entity_scope: list[str] | None = None,
) -> None:
    """HI-03: dual approver must be active AND must differ from
    actor + raiser + dispositioner (separation of duties)."""
    if str(dual_approver_user_id) == str(actor_user_id):
        raise AuthError(
            "dual_approver_invalid",
            "Dual approver must differ from the actor reopening the NCR.",
            403,
        )
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _scope_check_ncr(conn, ncr_no, entity_scope)
            await _assert_can_approve(conn, dual_approver_user_id, "dual_approver")
            row = await conn.fetchrow(
                """
                SELECT status, raised_by, disposition_by, concurrence_user_id
                  FROM ncr_record WHERE ncr_no = $1 FOR UPDATE
                """,
                ncr_no,
            )
            if row is None:
                raise AuthError("ncr_not_found", "NCR not found", 404)
            _check_status(row["status"], _ALLOW_REOPEN_FROM, "reopen")

            approver = str(dual_approver_user_id)
            for who, label in (
                (row["raised_by"], "raiser"),
                (row["disposition_by"], "dispositioner"),
                (row["concurrence_user_id"], "concurrer"),
            ):
                if who and str(who) == approver:
                    raise AuthError(
                        "dual_approver_invalid",
                        f"Dual approver cannot also be the {label}.",
                        403,
                    )

            # NEW-02 (option c): reopen does NOT overwrite dual_approval_by.
            # The audit log captures the named approver via the
            # `ncr_reopened` payload's `dual_approver_user_id` field, so
            # history is preserved. The header column reflects whatever
            # /dual-approve last recorded — callers can re-run /dual-approve
            # post-reopen to re-record an approver for the new lifecycle if
            # required by policy.
            await conn.execute(
                """
                UPDATE ncr_record SET
                    status            = 'raised',
                    reopen_reason     = $2,
                    reopened_at       = NOW(),
                    reopened_by       = $3,
                    closed_at         = NULL,
                    closure_tat_days  = NULL,
                    cancelled_at      = NULL,
                    cancelled_by      = NULL,
                    cancel_reason     = NULL
                 WHERE ncr_no = $1
                """,
                ncr_no, reason, actor_user_id,
            )
            await event_log.write(
                conn, ncr_no=ncr_no, event_type=event_log.EV_REOPENED,
                occurred_by=actor_user_id,
                payload={
                    "reason": reason,
                    "dual_approver_user_id": approver,
                },
            )


# ── 2.13 audit ───────────────────────────────────────────────────────────


async def audit(pool, *, ncr_no: str,
                entity_scope: list[str] | None = None) -> list[dict]:
    async with pool.acquire() as conn:
        await _scope_check_ncr(conn, ncr_no, entity_scope)
        # Touch the NCR first so we 404 cleanly when scope didn't (no scope set).
        row = await conn.fetchrow(
            "SELECT 1 FROM ncr_record WHERE ncr_no = $1", ncr_no,
        )
        if row is None:
            raise AuthError("ncr_not_found", "NCR not found", 404)

        rows = await conn.fetch(
            """
            SELECT event_id, event_type, entity_type, entity_id,
                   occurred_at, occurred_by, payload
              FROM ncr_event_log
             WHERE entity_id = $1
             ORDER BY occurred_at, event_id
            """,
            ncr_no,
        )
    return [
        {
            "event_id": int(r["event_id"]),
            "event_type": r["event_type"],
            "entity_type": r["entity_type"],
            "entity_id": r["entity_id"],
            "occurred_at": _iso(r["occurred_at"]),
            "occurred_by": r["occurred_by"],
            "payload": _parse_jsonb(r["payload"]) or {},
        }
        for r in rows
    ]
