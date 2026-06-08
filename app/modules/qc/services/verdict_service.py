"""QC Inward Inspection — verdict and override service.

Covers:
  set_verdict — record pass/fail verdict; blocks pass if out-of-spec readings present
  override    — qc_manager/admin can flip a prior verdict
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.core.middleware.request_context import AuthError
from app.modules.qc.services.audit import write_audit


# ── ISO helper ────────────────────────────────────────────────────────────


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


# ── set_verdict ───────────────────────────────────────────────────────────


async def set_verdict(
    pool,
    *,
    inspection_id: int,
    verdict: str,
    accepted_qty: float | None,
    rejected_qty: float | None,
    summary_remarks: str | None,
    actor_user_id: int,
    actor_role: str | None = None,
    is_admin: bool = False,
) -> dict:
    """Record a pass/fail verdict on an inspection.

    The verdict IS the manager's approval/sign-off — only qc_manager or admin
    may set it, and they may re-decide after an inspection is already approved.

    Raises 409 if verdict == 'passed' and out-of-spec readings exist.
    Generates a placeholder NCR number on failure.
    """
    if not (is_admin or actor_role == "qc_manager"):
        raise AuthError(
            "permission_denied",
            "Only a QC manager can set the verdict.",
            403,
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            insp = await conn.fetchrow(
                "SELECT status, verdict, remarks FROM qc_inward_inspection "
                "WHERE inspection_id = $1 FOR UPDATE",
                inspection_id,
            )
            if insp is None:
                raise AuthError(
                    "inspection_not_found",
                    f"Inspection {inspection_id} not found.",
                    404,
                )

            # Guard: readings must be submitted first; but a manager may
            # re-decide a verdict that has already been recorded (approved).
            if insp["status"] not in (
                "readings_submitted", "verdict_passed", "verdict_failed",
            ):
                raise AuthError(
                    "invalid_state",
                    "Submit readings before setting a verdict.",
                    409,
                )

            # Guard: check for out-of-spec readings when passing
            if verdict == "passed":
                oos_count: int = int(
                    await conn.fetchval(
                        """
                        SELECT COUNT(*) FROM qc_inward_reading
                         WHERE inspection_id = $1 AND is_within_spec IS FALSE
                        """,
                        inspection_id,
                    ) or 0
                )
                if oos_count > 0:
                    raise AuthError(
                        "out_of_spec_readings_present",
                        f"Cannot pass — {oos_count} out-of-spec reading(s). "
                        "Use override.",
                        409,
                    )

            now = datetime.now(timezone.utc)
            new_status = "verdict_passed" if verdict == "passed" else "verdict_failed"

            # Append summary_remarks to existing remarks
            combined_remarks: str | None = insp["remarks"]
            if summary_remarks:
                combined_remarks = (
                    f"{combined_remarks}\n{summary_remarks}"
                    if combined_remarks
                    else summary_remarks
                )

            # Generate NCR placeholder on failure
            ncr_no: str | None = None
            if verdict == "failed":
                date_str = now.strftime("%Y%m%d")
                ncr_no = f"NCR-{date_str}-{inspection_id}"
                # TODO: wire real NCR raise via app.modules.ncr when that
                # integration is ready. For now we only persist the placeholder.

            # Resolve the approver's display name for the approval record.
            approver_row = await conn.fetchrow(
                "SELECT full_name FROM auth_user WHERE user_id = $1", actor_user_id
            )
            approver_name: str | None = (
                approver_row["full_name"] if approver_row else str(actor_user_id)
            )

            await conn.execute(
                """
                UPDATE qc_inward_inspection SET
                    verdict          = $2,
                    status           = $3,
                    verdict_at       = $4,
                    accepted_qty     = COALESCE($5, accepted_qty),
                    rejected_qty     = COALESCE($6, rejected_qty),
                    remarks          = COALESCE($7, remarks),
                    ncr_no           = COALESCE($8, ncr_no),
                    approved_by      = $9,
                    approved_by_name = $10,
                    approved_at      = $4
                 WHERE inspection_id = $1
                """,
                inspection_id,
                verdict,
                new_status,
                now,
                accepted_qty,
                rejected_qty,
                combined_remarks,
                ncr_no,
                actor_user_id,
                approver_name,
            )

            event_type = "verdict_passed" if verdict == "passed" else "verdict_failed"
            await write_audit(
                conn,
                inspection_id=inspection_id,
                event_type=event_type,
                from_state=insp["status"],
                to_state=new_status,
                actor_user_id=actor_user_id,
                payload_diff={
                    "verdict": verdict,
                    "accepted_qty": accepted_qty,
                    "rejected_qty": rejected_qty,
                    "ncr_no": ncr_no,
                },
            )

    next_step = "Generate RM report" if verdict == "passed" else "NCR raised"
    return {"verdict": verdict, "next_step": next_step}


# ── override ──────────────────────────────────────────────────────────────


async def override(
    pool,
    *,
    inspection_id: int,
    new_verdict: str,
    reason: str,
    actor_user_id: int,
    actor_role: str | None,
) -> dict:
    """Override a prior verdict.  Requires qc_manager or admin role."""
    allowed_roles = {"qc_manager", "admin"}
    if actor_role not in allowed_roles:
        raise AuthError(
            "permission_denied",
            "Override requires qc_manager or admin.",
            403,
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            insp = await conn.fetchrow(
                "SELECT status, verdict, started_by_name FROM qc_inward_inspection "
                "WHERE inspection_id = $1 FOR UPDATE",
                inspection_id,
            )
            if insp is None:
                raise AuthError(
                    "inspection_not_found",
                    f"Inspection {inspection_id} not found.",
                    404,
                )

            old_verdict: str | None = insp["verdict"]
            new_status = "verdict_passed" if new_verdict == "passed" else "verdict_failed"

            # Look up the name of the actor for the override_name column
            actor_row = await conn.fetchrow(
                "SELECT full_name FROM auth_user WHERE user_id = $1",
                actor_user_id,
            )
            actor_name: str | None = actor_row["full_name"] if actor_row else str(actor_user_id)

            now = datetime.now(timezone.utc)
            await conn.execute(
                """
                UPDATE qc_inward_inspection SET
                    verdict                    = $2,
                    status                     = $3,
                    verdict_overridden_by      = $4,
                    verdict_overridden_by_name = $5,
                    override_reason            = $6,
                    approved_by                = $4,
                    approved_by_name           = $5,
                    approved_at                = $7
                 WHERE inspection_id = $1
                """,
                inspection_id,
                new_verdict,
                new_status,
                actor_user_id,
                actor_name,
                reason,
                now,
            )

            await write_audit(
                conn,
                inspection_id=inspection_id,
                event_type="verdict_overridden",
                from_state=insp["status"],
                to_state=new_status,
                actor_user_id=actor_user_id,
                payload_diff={
                    "old_verdict": old_verdict,
                    "new_verdict": new_verdict,
                    "override_reason": reason,
                },
            )

    return {"old_verdict": old_verdict, "new_verdict": new_verdict}
