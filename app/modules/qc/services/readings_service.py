"""QC Inward Inspection — parameter readings service.

Covers:
  add_batch       — batch-insert readings with auto spec-evaluation
  update_reading  — partial update + re-evaluation
  delete_reading  — soft-delete (physical DELETE + audit)
  _evaluate       — pure compliance / severity / deviation computation
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.core.middleware.request_context import AuthError
from app.modules.qc.services.audit import write_audit


# ── Reading-mutation access gate ───────────────────────────────────────────

_FINALIZED_STATUSES = ("verdict_passed", "verdict_failed")


def _enforce_reading_mutation(
    status: str | None, *, actor_role: str | None, is_admin: bool
) -> None:
    """Gate add/edit/delete of readings by inspection state + role.

    - Open (in_progress / readings_submitted): anyone holding qc.inspection.edit
      (inspector or manager) may modify.
    - Approved (verdict set): ONLY qc_manager / admin.
    - Cancelled: locked for everyone — reopen first.
    """
    is_manager = is_admin or actor_role == "qc_manager"
    if status in _FINALIZED_STATUSES:
        if not is_manager:
            raise AuthError(
                "approval_locked",
                "This inspection is approved — only a QC manager can add, "
                "edit, or delete readings.",
                403,
            )
        return
    if status == "cancelled":
        raise AuthError(
            "invalid_state",
            "Cannot modify readings on a cancelled inspection.",
            409,
        )


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


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── _evaluate ─────────────────────────────────────────────────────────────


def _evaluate(
    num: float | None,
    smin: float | None,
    smax: float | None,
    starget: float | None,
) -> tuple[bool | None, str | None, float | None]:
    """Evaluate a numeric reading against its spec.

    Returns (is_within_spec, severity, deviation_pct).

    Deviation is the relative distance beyond the violated bound:
        excess   = how far num is outside the violated bound
        divisor  = |bound| if non-zero, else |band| if non-zero, else |target|
                   if non-zero, else 1.0 (fallback to avoid zero-division)
        deviation_pct = |excess| / divisor * 100
    Severity thresholds: <=10 % → minor, <=25 % → major, else → critical.
    """
    if num is None:
        return (None, None, None)

    has_min = smin is not None
    has_max = smax is not None

    if not has_min and not has_max:
        return (None, None, None)

    within = (not has_min or num >= smin) and (not has_max or num <= smax)  # type: ignore[operator]
    if within:
        return (True, None, 0.0)

    # Determine the excess and the violated bound value
    if has_min and num < smin:  # type: ignore[operator]
        excess = smin - num  # type: ignore[operator]
        violated_bound: float = smin  # type: ignore[assignment]
    else:
        # num > smax
        excess = num - smax  # type: ignore[operator]
        violated_bound = smax  # type: ignore[assignment]

    # Choose denominator (sane non-zero fallback chain)
    if abs(violated_bound) > 1e-12:
        divisor: float = abs(violated_bound)
    elif has_min and has_max:
        band = abs(smax - smin)  # type: ignore[operator]
        divisor = band if band > 1e-12 else (abs(starget) if starget and abs(starget) > 1e-12 else 1.0)
    elif starget is not None and abs(starget) > 1e-12:
        divisor = abs(starget)
    else:
        divisor = 1.0

    deviation_pct = round(abs(excess) / divisor * 100, 2)

    if deviation_pct <= 10.0:
        severity = "minor"
    elif deviation_pct <= 25.0:
        severity = "major"
    else:
        severity = "critical"

    return (False, severity, deviation_pct)


# ── row → dict helper ─────────────────────────────────────────────────────


def _reading_row_to_dict(row) -> dict:
    return {
        "reading_id": row["reading_id"],
        "inspection_id": row["inspection_id"],
        "parameter_id": row["parameter_id"],
        "parameter_name": row["parameter_name"],
        "parameter_unit": row["parameter_unit"],
        "observed_value_num": _f(row["observed_value_num"]),
        "observed_value_text": row["observed_value_text"],
        "spec_min": _f(row["spec_min"]),
        "spec_max": _f(row["spec_max"]),
        "spec_target": _f(row["spec_target"]),
        "is_within_spec": row["is_within_spec"],
        "severity": row["severity"],
        "deviation_pct": _f(row["deviation_pct"]),
        "method": row["method"],
        "instrument": row["instrument"],
        "notes": row["notes"],
        "recorded_at": _iso(row["recorded_at"]),
    }


# ── add_batch ─────────────────────────────────────────────────────────────


async def add_batch(
    pool,
    *,
    inspection_id: int,
    readings: list[dict],
    actor_user_id: int,
    actor_role: str | None = None,
    is_admin: bool = False,
) -> dict:
    """Batch-insert readings, evaluate compliance, advance status.

    Returns {inserted_count, out_of_spec_count}.
    """
    async with pool.acquire() as conn:
        # Validate inspection exists and is open
        insp = await conn.fetchrow(
            "SELECT status, sku_id FROM qc_inward_inspection WHERE inspection_id = $1",
            inspection_id,
        )
        if insp is None:
            raise AuthError(
                "inspection_not_found",
                f"Inspection {inspection_id} not found.",
                404,
            )

        # Gate by state + role (post-approval = manager only).
        _enforce_reading_mutation(
            insp["status"], actor_role=actor_role, is_admin=is_admin
        )

        sku_id: int | None = insp["sku_id"]

        async with conn.transaction():
            inserted = 0
            out_of_spec = 0
            now = datetime.now(timezone.utc)

            for reading_input in readings:
                parameter_id: int = reading_input["parameter_id"]

                # Fetch parameter metadata
                param_row = await conn.fetchrow(
                    "SELECT name, unit FROM qc_parameter WHERE parameter_id = $1",
                    parameter_id,
                )
                param_name: str | None = param_row["name"] if param_row else None
                param_unit: str | None = param_row["unit"] if param_row else None

                # Fetch spec snapshot
                spec_row = await conn.fetchrow(
                    """
                    SELECT spec_min, spec_max, spec_target
                      FROM qc_sku_spec
                     WHERE sku_id = $1 AND parameter_id = $2
                    """,
                    sku_id,
                    parameter_id,
                ) if sku_id is not None else None

                spec_min: float | None = _f(spec_row["spec_min"]) if spec_row else None
                spec_max: float | None = _f(spec_row["spec_max"]) if spec_row else None
                spec_target: float | None = _f(spec_row["spec_target"]) if spec_row else None

                # Evaluate compliance
                num = reading_input.get("observed_value_num")
                is_within_spec, severity, deviation_pct = _evaluate(
                    num, spec_min, spec_max, spec_target
                )

                if is_within_spec is False:
                    out_of_spec += 1

                # reading_id is an app-supplied 8-digit time-based BIGINT.
                # Each insert is awaited before the next loop iteration, so the
                # closure safely reads the current loop values; PK retry
                # regenerates the id on the rare same-millisecond collision.
                async def _insert_reading():
                    return await conn.execute(
                        """
                        INSERT INTO qc_inward_reading (
                            reading_id,
                            inspection_id, parameter_id, parameter_name, parameter_unit,
                            observed_value_num, observed_value_text,
                            spec_min, spec_max, spec_target,
                            is_within_spec, severity, deviation_pct,
                            method, instrument, notes, recorded_at
                        )
                        VALUES (
                            $1,
                            $2, $3, $4, $5,
                            $6, $7,
                            $8, $9, $10,
                            $11, $12, $13,
                            $14, $15, $16, $17
                        )
                        """,
                        new_short_time_id(),
                        inspection_id,
                        parameter_id,
                        param_name,
                        param_unit,
                        num,
                        reading_input.get("observed_value_text"),
                        spec_min,
                        spec_max,
                        spec_target,
                        is_within_spec,
                        severity,
                        deviation_pct,
                        reading_input.get("method"),
                        reading_input.get("instrument"),
                        reading_input.get("notes"),
                        now,
                    )

                await insert_with_pk_retry(conn, _insert_reading)
                inserted += 1

            # Advance status only from in_progress. A manager adding readings to
            # an already-approved inspection must NOT downgrade its status.
            advanced = insp["status"] == "in_progress"
            if advanced:
                await conn.execute(
                    """
                    UPDATE qc_inward_inspection
                       SET status = 'readings_submitted'
                     WHERE inspection_id = $1
                    """,
                    inspection_id,
                )

            resulting_status = "readings_submitted" if advanced else insp["status"]
            await write_audit(
                conn,
                inspection_id=inspection_id,
                # Only label it a state transition when one actually occurred.
                event_type="readings_submitted" if advanced else "readings_added",
                from_state=insp["status"],
                to_state=resulting_status,
                actor_user_id=actor_user_id,
                payload_diff={
                    "inserted_count": inserted,
                    "out_of_spec_count": out_of_spec,
                },
            )

    return {"inserted_count": inserted, "out_of_spec_count": out_of_spec}


# ── update_reading ────────────────────────────────────────────────────────


async def update_reading(
    pool,
    *,
    inspection_id: int,
    reading_id: int,
    fields: dict,
    actor_user_id: int,
    actor_role: str | None = None,
    is_admin: bool = False,
) -> dict:
    """Partial update of a reading; re-evaluates compliance when value changes."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT r.*, i.status AS insp_status
                  FROM qc_inward_reading r
                  JOIN qc_inward_inspection i USING (inspection_id)
                 WHERE r.reading_id = $1 AND r.inspection_id = $2
                   FOR UPDATE OF r
                """,
                reading_id,
                inspection_id,
            )
            if row is None:
                raise AuthError(
                    "reading_not_found",
                    f"Reading {reading_id} not found on inspection {inspection_id}.",
                    404,
                )

            # Gate by state + role (post-approval = manager only).
            _enforce_reading_mutation(
                row["insp_status"], actor_role=actor_role, is_admin=is_admin
            )

            # Build SET clauses
            updates: list[str] = []
            params: list[Any] = []
            pidx = 0

            def add_col(col: str, val: Any) -> None:
                nonlocal pidx
                pidx += 1
                updates.append(f"{col} = ${pidx}")
                params.append(val)

            # Apply incoming fields
            new_num = fields.get("observed_value_num", row["observed_value_num"])
            value_changed = (
                "observed_value_num" in fields
                and fields["observed_value_num"] != _f(row["observed_value_num"])
            )

            for col in ("observed_value_num", "observed_value_text",
                        "method", "instrument", "notes"):
                if col in fields:
                    add_col(col, fields[col])

            # Re-evaluate if numeric value changed
            if value_changed:
                spec_min = _f(row["spec_min"])
                spec_max = _f(row["spec_max"])
                spec_target = _f(row["spec_target"])
                is_within_spec, severity, deviation_pct = _evaluate(
                    new_num, spec_min, spec_max, spec_target
                )
                add_col("is_within_spec", is_within_spec)
                add_col("severity", severity)
                add_col("deviation_pct", deviation_pct)

            if updates:
                pidx += 1
                sql = (
                    f"UPDATE qc_inward_reading SET {', '.join(updates)} "
                    f"WHERE reading_id = ${pidx} RETURNING *"
                )
                params.append(reading_id)
                updated_row = await conn.fetchrow(sql, *params)
            else:
                updated_row = row

            await write_audit(
                conn,
                inspection_id=inspection_id,
                event_type="reading_updated",
                from_state=None,
                to_state=None,
                actor_user_id=actor_user_id,
                payload_diff={"reading_id": reading_id, "fields_changed": list(fields.keys())},
            )

    return _reading_row_to_dict(updated_row)


# ── delete_reading ────────────────────────────────────────────────────────


async def delete_reading(
    pool,
    *,
    inspection_id: int,
    reading_id: int,
    reason: str | None,
    actor_user_id: int,
    actor_role: str | None = None,
    is_admin: bool = False,
) -> None:
    """Delete a reading and write an audit event."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT r.reading_id, i.status AS insp_status
                  FROM qc_inward_reading r
                  JOIN qc_inward_inspection i USING (inspection_id)
                 WHERE r.reading_id = $1 AND r.inspection_id = $2
                   FOR UPDATE OF r
                """,
                reading_id,
                inspection_id,
            )
            if row is None:
                raise AuthError(
                    "reading_not_found",
                    f"Reading {reading_id} not found on inspection {inspection_id}.",
                    404,
                )

            # Gate by state + role (post-approval = manager only).
            _enforce_reading_mutation(
                row["insp_status"], actor_role=actor_role, is_admin=is_admin
            )

            await conn.execute(
                "DELETE FROM qc_inward_reading WHERE reading_id = $1",
                reading_id,
            )

            await write_audit(
                conn,
                inspection_id=inspection_id,
                event_type="reading_deleted",
                from_state=None,
                to_state=None,
                actor_user_id=actor_user_id,
                payload_diff={"reading_id": reading_id, "reason": reason},
            )
