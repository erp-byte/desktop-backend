"""Closing a batch / completing a job card whose accounting doesn't balance.

The allowed difference is the BOM's tolerance (bom_header.
allowed_balance_tolerance_pct, a FRACTION, 0.001 = 0.1% by design) times the
batch's input, or a fixed 0.05 kg when there is no input to take a share of.

When the figures still don't balance, an admin or a floor manager may close /
complete anyway, but only with a typed reason. The override is recorded as a
'deviation' remark on the job card under the logged-in user's name, so the Remarks
tab shows who did it, why, and how far off it was. Everyone else is told how far
off it is, how much is allowed, and who can complete it.
"""
from __future__ import annotations

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.modules.production.services.jc_accounting_v2 import (
    BALANCE_TOLERANCE_PCT_DEF,
    BALANCE_TOLERANCE_QTY,
)

OVERRIDE_ROLE = "floor_manager"
MIN_REASON_LEN = 5


def allowed_difference_kg(total_input, tolerance_pct) -> float:
    """How far off (kg) still counts as balanced — the same rule the
    accounting save applies (jc_accounting_v2.save_accounting)."""
    total = float(total_input or 0)
    pct = float(tolerance_pct) if tolerance_pct is not None else BALANCE_TOLERANCE_PCT_DEF
    return total * pct if total > 0 else BALANCE_TOLERANCE_QTY


def unbalanced_message(diff_kg, allowed_kg, *, what: str = "This batch") -> str:
    return (
        f"{what} doesn't balance: it is off by {abs(float(diff_kg or 0)):.3f} kg, "
        f"and {float(allowed_kg):.3f} kg is allowed. Correct the figures in "
        "Accounting, or an admin or floor manager can complete it anyway with a reason."
    )


def clean_reason(reason: str | None) -> str | None:
    """The reason as typed (trimmed), or None when it says nothing."""
    text = (reason or "").strip()
    return text if len(text) >= MIN_REASON_LEN else None


def override_remark(diff_kg, allowed_kg, reason: str | None, *, what: str,
                    action: str = "completed") -> str:
    if diff_kg is None:
        # Closed by override with nothing saved to check — don't claim a number.
        return (f"{what} {action} without a saved balance to check against. "
                f"Reason: {reason or 'not given'}")
    allowed = f"; {float(allowed_kg):.3f} kg allowed" if allowed_kg is not None else ""
    return (
        f"{what} {action} although it doesn't balance "
        f"(off by {abs(float(diff_kg or 0)):.3f} kg{allowed}). "
        f"Reason: {reason or 'not given'}"
    )


async def batch_balance_check(conn, *, job_card_id: int, batch_id: int | None,
                              batch_number, ui_is_balanced: bool | None,
                              ui_difference_kg: float | None,
                              allow_unbalanced: bool) -> tuple[dict | None, dict | None]:
    """The balance lock on closing one batch.

    Judged on the saved per-batch accounting (job_card_accounting_v2 — what
    completing the job card checks too); the screen's own verdict counts only
    when nothing is saved yet. Returns (refusal, override):
      * refusal — the error to return: not balanced and no override;
      * override — {"difference_kg", "allowed_kg"} when it doesn't balance but
        `allow_unbalanced` (an admin / floor manager with a reason) lets it close.
    Both None when it balances.
    """
    acct = await conn.fetchrow(
        "SELECT is_balanced, balance_difference_qty, total_input_qty "
        "FROM   job_card_accounting_v2 "
        "WHERE  job_card_id=$1 AND COALESCE(batch_id,0)=COALESCE($2,0)",
        job_card_id, batch_id,
    )
    balanced = acct["is_balanced"] if acct else ui_is_balanced
    if balanced is True:
        return None, None
    what = f"Batch {batch_number}" if batch_number is not None else "This batch"
    if acct is None:
        # Nothing saved to judge against (and the screen didn't say balanced):
        # the allowed amount depends on the input, which isn't known here.
        if allow_unbalanced:
            # Only what the screen said, if anything — never an invented 0 kg.
            return None, {"difference_kg": (float(ui_difference_kg)
                                            if ui_difference_kg is not None else None),
                          "allowed_kg": None}
        return {
            "error": "batch_unbalanced",
            "is_balanced": (bool(balanced) if balanced is not None else None),
            "balance_difference_qty": ui_difference_kg,
            "message": (
                f"{what} has no saved accounting to check the balance against. "
                "Save the Accounting figures first."
            ),
        }, None
    diff = float(acct["balance_difference_qty"] or 0)
    allowed = allowed_difference_kg(acct["total_input_qty"],
                                    await tolerance_for_job_card(conn, job_card_id))
    if allow_unbalanced:
        return None, {"difference_kg": diff, "allowed_kg": allowed}
    return {
        "error": "batch_unbalanced",
        "is_balanced": (bool(balanced) if balanced is not None else None),
        "balance_difference_qty": diff,
        "allowed_difference_kg": round(allowed, 3),
        "message": unbalanced_message(diff, allowed, what=what),
    }, None


async def can_override_balance(conn, user) -> bool:
    """Admins, and anyone holding the floor-manager role among ALL their roles."""
    if getattr(user, "is_admin", False):
        return True
    role_ids = [r for r in (getattr(user, "role_ids", None) or []) if r is not None]
    if not role_ids:
        return False
    return bool(await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM auth_role WHERE role_id = ANY($1) "
        "AND lower(trim(role_name)) = $2)",
        role_ids, OVERRIDE_ROLE,
    ))


async def tolerance_for_job_card(conn, job_card_id: int):
    """The job card's BOM tolerance (None when it has no BOM)."""
    return await conn.fetchval(
        "SELECT h.allowed_balance_tolerance_pct FROM job_card_v2 j "
        "JOIN bom_header h ON h.bom_id = j.bom_id WHERE j.job_card_id = $1",
        job_card_id,
    )


async def record_override_remark(conn, *, job_card_id: int, content: str,
                                 recorded_by: str | None) -> None:
    async def _insert():
        return await conn.fetchrow(
            """
            INSERT INTO job_card_remarks_v2
                (remark_id, job_card_id, remark_type, content, recorded_by)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING remark_id
            """,
            new_short_time_id(), job_card_id, "deviation", content, recorded_by,
        )
    await insert_with_pk_retry(conn, _insert)
