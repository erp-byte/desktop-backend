"""NCR (Non-Conformance Report) — service layer.

One table (ncr_record); the two 1:N detail sets are folded into JSONB columns.

  create        — create an NCR; optionally prefilled from a failed inspection
  list_ncr      — paginated list with filters
  get_detail    — full record incl. parsed JSONB detail arrays
  update        — partial update + severity/closure recompute + status guard
  delete        — hard delete (no child tables)
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from asyncpg.exceptions import UniqueViolationError

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.core.middleware.request_context import AuthError

_SEVERITY_RANK = {"minor": 1, "major": 2, "critical": 3}
_STATUS_FLOW = {
    "open": {"open", "in_supplier_action", "closed"},
    "in_supplier_action": {"in_supplier_action", "closed", "open"},
    "closed": {"closed", "open"},  # allow re-open
}


# ── ISO / parse helpers ───────────────────────────────────────────────────


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


def _jsonb(v: Any) -> list:
    """asyncpg returns JSONB as str (no codec) or already-parsed list."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            return []
    return []


def _rollup_severity(failed_parameters: list[dict]) -> str | None:
    """Highest severity across the failed parameters (critical > major > minor)."""
    best = 0
    for fp in failed_parameters:
        rank = _SEVERITY_RANK.get((fp or {}).get("severity") or "", 0)
        best = max(best, rank)
    for name, rank in _SEVERITY_RANK.items():
        if rank == best:
            return name
    return None


# ── Prefill from a failed inspection ──────────────────────────────────────


async def _prefill_from_inspection(conn, inspection_id: int) -> dict:
    """Return header + failed_parameters prefill derived from an inspection and
    its out-of-spec readings. Raises 404 if the inspection is missing."""
    insp = await conn.fetchrow(
        "SELECT * FROM qc_inward_inspection WHERE inspection_id = $1", inspection_id
    )
    if insp is None:
        raise AuthError("inspection_not_found", f"Inspection {inspection_id} not found.", 404)

    readings = await conn.fetch(
        """
        SELECT r.reading_id, r.parameter_id, p.code AS param_code,
               r.observed_value_num, r.observed_value_text,
               r.spec_min, r.spec_max, r.spec_target, r.severity
          FROM qc_inward_reading r
          LEFT JOIN qc_parameter p ON p.parameter_id = r.parameter_id
         WHERE r.inspection_id = $1 AND r.is_within_spec IS FALSE
         ORDER BY r.reading_id
        """,
        inspection_id,
    )

    failed: list[dict] = []
    for r in readings:
        num = r["observed_value_num"]
        smin, smax = r["spec_min"], r["spec_max"]
        if num is not None and smax is not None and float(num) > float(smax):
            deviation = "above_max"
        elif num is not None and smin is not None and float(num) < float(smin):
            deviation = "below_min"
        elif r["observed_value_text"]:
            deviation = "presence"
        else:
            deviation = "above_max" if num is not None else None
        spec_value = r["spec_target"]
        if spec_value is None:
            spec_value = smax if smax is not None else smin
        failed.append(
            {
                "param_code": r["param_code"],
                "reading_id": r["reading_id"],
                "spec_value": float(spec_value) if spec_value is not None else None,
                "actual_value": float(num) if num is not None else None,
                "deviation_type": deviation,
                "severity": r["severity"] or "major",
                "quantity_impacted_kg": None,
                "disposition": None,
            }
        )

    keys = insp.keys()
    return {
        "supplier_id": insp["supplier_id"] if "supplier_id" in keys else None,
        "supplier_name": insp["supplier_name"] if "supplier_name" in keys else None,
        "transaction_no": insp["transaction_no"] if "transaction_no" in keys else None,
        "product_description": insp["sku_name"] if "sku_name" in keys else None,
        "batch_no": insp["lot_number"] if "lot_number" in keys else None,
        "quantity": (
            float(insp["rejected_qty"])
            if "rejected_qty" in keys and insp["rejected_qty"] is not None
            else None
        ),
        "failed_parameters": failed,
    }


async def _next_ncr_no(conn, year: int) -> str:
    row = await conn.fetchrow(
        """
        SELECT COALESCE(MAX( (split_part(ncr_no, '/', 3))::int ), 0) + 1 AS seq
          FROM ncr_record
         WHERE ncr_no ~ $1
        """,
        f"^NCR/{year}/[0-9]+$",
    )
    seq = row["seq"] if row and row["seq"] else 1
    return f"NCR/{year}/{seq:04d}"


# ── Create ─────────────────────────────────────────────────────────────────


async def create(pool, *, body: dict, actor_user_id: int | None) -> dict:
    """Create an NCR. If body['from_inspection_id'] is set, prefill from that
    inspection; caller-supplied fields override the prefill."""
    from_inspection_id = body.get("from_inspection_id")
    today = datetime.now(timezone.utc).date()

    async with pool.acquire() as conn:
        async with conn.transaction():
            prefill: dict = {}
            if from_inspection_id is not None:
                prefill = await _prefill_from_inspection(conn, from_inspection_id)

            def pick(field: str, default=None):
                v = body.get(field)
                if v is not None:
                    return v
                return prefill.get(field, default)

            failed_parameters = body.get("failed_parameters")
            if failed_parameters is None:
                failed_parameters = prefill.get("failed_parameters", [])
            supplier_actions = body.get("supplier_actions") or []

            documented_date = body.get("documented_date") or today.isoformat()
            try:
                doc_year = date.fromisoformat(documented_date).year
            except (TypeError, ValueError):
                doc_year = today.year

            severity_rollup = _rollup_severity(failed_parameters)
            caller_ncr_no = body.get("ncr_no")

            # ncr_id (PK) collisions are resolved inside insert_with_pk_retry.
            # ncr_no is UNIQUE but NOT the PK, so its collisions (concurrent
            # auto-numbering, or a duplicate caller value) are handled here by
            # regenerating a fresh number and retrying within the same txn.
            row = None
            for attempt in range(5):
                ncr_no = caller_ncr_no or await _next_ncr_no(conn, doc_year)

                async def _insert(_ncr_no=ncr_no):
                    return await conn.fetchrow(
                        """
                        INSERT INTO ncr_record (
                            ncr_id, ncr_no,
                            supplier_id, supplier_name, other_party, detected_by,
                            invoice_challan_ref, batch_no, transaction_no, line_number,
                            product_description, rc_no, quantity,
                            reason_nonconformity, food_safety_flag, documented_by, documented_date,
                            disposition, financial_action, supplier_action_type, target_completion_date,
                            severity_rollup, status,
                            failed_parameters, supplier_actions
                        )
                        VALUES (
                            $1, $2,
                            $3, $4, $5, $6,
                            $7, $8, $9, $10,
                            $11, $12, $13,
                            $14, $15, $16, $17,
                            $18, $19, $20, $21,
                            $22, 'open',
                            $23::jsonb, $24::jsonb
                        )
                        RETURNING ncr_id, ncr_no
                        """,
                        new_short_time_id(),
                        _ncr_no,
                        pick("supplier_id"),
                        pick("supplier_name"),
                        body.get("other_party"),
                        body.get("detected_by") if body.get("detected_by") is not None else actor_user_id,
                        body.get("invoice_challan_ref"),
                        pick("batch_no"),
                        pick("transaction_no"),
                        body.get("line_number"),
                        pick("product_description"),
                        body.get("rc_no"),
                        pick("quantity"),
                        body.get("reason_nonconformity"),
                        bool(body.get("food_safety_flag")) if body.get("food_safety_flag") is not None else False,
                        body.get("documented_by") if body.get("documented_by") is not None else actor_user_id,
                        documented_date,
                        body.get("disposition"),
                        body.get("financial_action"),
                        body.get("supplier_action_type"),
                        body.get("target_completion_date"),
                        severity_rollup,
                        json.dumps(failed_parameters),
                        json.dumps(supplier_actions),
                    )

                try:
                    row = await insert_with_pk_retry(conn, _insert)
                    break
                except UniqueViolationError as exc:
                    constraint = getattr(exc, "constraint_name", "") or ""
                    if "ncr_no" not in constraint:
                        raise
                    if caller_ncr_no:
                        raise AuthError(
                            "ncr_no_exists",
                            f"NCR number '{caller_ncr_no}' already exists.",
                            409,
                        ) from exc
                    if attempt == 4:
                        raise  # exhausted auto-number retries
                    # else: regenerate ncr_no and retry

            # Back-link the inspection to its NCR (only if not already set).
            if from_inspection_id is not None:
                await conn.execute(
                    """
                    UPDATE qc_inward_inspection
                       SET ncr_no = $1
                     WHERE inspection_id = $2 AND ncr_no IS NULL
                    """,
                    row["ncr_no"],
                    from_inspection_id,
                )

    return {"ncr_id": row["ncr_id"], "ncr_no": row["ncr_no"]}


# ── List ───────────────────────────────────────────────────────────────────


async def list_ncr(pool, *, filters: dict, page: int, page_size: int) -> dict:
    where = []
    args: list[Any] = []

    def add(clause: str, value: Any):
        args.append(value)
        where.append(clause.format(n=len(args)))

    if filters.get("status"):
        add("status = ${n}", filters["status"])
    if filters.get("supplier_id") is not None:
        add("supplier_id = ${n}", int(filters["supplier_id"]))
    if filters.get("transaction_no"):
        add("transaction_no = ${n}", filters["transaction_no"])
    if filters.get("q"):
        add(
            "(ncr_no ILIKE ${n} OR product_description ILIKE ${n} OR supplier_name ILIKE ${n})",
            f"%{filters['q']}%",
        )

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM ncr_record{where_sql}", *args)
        offset = (page - 1) * page_size
        rows = await conn.fetch(
            f"""
            SELECT ncr_id, ncr_no, supplier_id, supplier_name, transaction_no,
                   product_description, status, severity_rollup, disposition,
                   food_safety_flag, documented_date, created_at,
                   COALESCE(jsonb_array_length(failed_parameters), 0) AS param_count
              FROM ncr_record{where_sql}
             ORDER BY created_at DESC, ncr_id DESC
             LIMIT {page_size} OFFSET {offset}
            """,
            *args,
        )

    items = [
        {
            "ncr_id": r["ncr_id"],
            "ncr_no": r["ncr_no"],
            "supplier_id": r["supplier_id"],
            "supplier_name": r["supplier_name"],
            "transaction_no": r["transaction_no"],
            "product_description": r["product_description"],
            "status": r["status"],
            "severity_rollup": r["severity_rollup"],
            "disposition": r["disposition"],
            "food_safety_flag": r["food_safety_flag"],
            "documented_date": _iso(r["documented_date"]),
            "created_at": _iso(r["created_at"]),
            "param_count": r["param_count"],
        }
        for r in rows
    ]
    total_pages = max(1, (total + page_size - 1) // page_size)
    return {
        "items": items,
        "total": total,
        "total_pages": total_pages,
        "page": page,
        "page_size": page_size,
    }


# ── Detail ─────────────────────────────────────────────────────────────────


async def get_detail(pool, ncr_id: int) -> dict | None:
    async with pool.acquire() as conn:
        r = await conn.fetchrow("SELECT * FROM ncr_record WHERE ncr_id = $1", ncr_id)
    if r is None:
        return None
    d = dict(r)
    out = {k: d.get(k) for k in d}
    # ISO-ify temporal fields
    for k in (
        "documented_date", "target_completion_date", "authorized_date",
        "received_date", "created_at",
    ):
        out[k] = _iso(d.get(k))
    out["quantity"] = float(d["quantity"]) if d.get("quantity") is not None else None
    out["financial_impact_inr"] = (
        float(d["financial_impact_inr"]) if d.get("financial_impact_inr") is not None else None
    )
    out["failed_parameters"] = _jsonb(d.get("failed_parameters"))
    out["supplier_actions"] = _jsonb(d.get("supplier_actions"))
    return out


# ── Update ─────────────────────────────────────────────────────────────────

_SCALAR_FIELDS = (
    "supplier_id", "supplier_name", "other_party", "detected_by",
    "invoice_challan_ref", "batch_no", "transaction_no", "line_number",
    "product_description", "rc_no", "quantity", "reason_nonconformity",
    "food_safety_flag", "documented_by", "documented_date", "disposition",
    "financial_action", "supplier_action_type", "target_completion_date",
    "authorized_person", "authorized_date", "received_by", "received_date",
    "approved_by", "ncr_category", "financial_impact_inr",
)


async def update(pool, *, ncr_id: int, fields: dict) -> dict:
    """Partial update. Recomputes severity_rollup when failed_parameters change;
    sets closure_tat_days when status transitions to 'closed'; guards transitions."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            cur = await conn.fetchrow(
                "SELECT status, documented_date, closure_tat_days FROM ncr_record WHERE ncr_id = $1 FOR UPDATE",
                ncr_id,
            )
            if cur is None:
                raise AuthError("ncr_not_found", f"NCR {ncr_id} not found.", 404)

            sets: list[str] = []
            args: list[Any] = []

            def put(col: str, value: Any, cast: str = ""):
                args.append(value)
                sets.append(f"{col} = ${len(args)}{cast}")

            for f in _SCALAR_FIELDS:
                if f in fields:
                    put(f, fields[f])

            # Only replace the JSONB arrays when a real list is supplied — an
            # explicit null is treated as "no change" (NOT a wipe). Send [] to
            # clear deliberately.
            if fields.get("failed_parameters") is not None:
                fp = fields["failed_parameters"]
                put("failed_parameters", json.dumps(fp), "::jsonb")
                put("severity_rollup", _rollup_severity(fp))
            if fields.get("supplier_actions") is not None:
                put("supplier_actions", json.dumps(fields["supplier_actions"]), "::jsonb")

            if "status" in fields and fields["status"]:
                new_status = fields["status"]
                old_status = cur["status"]
                if new_status not in _STATUS_FLOW.get(old_status, set()):
                    raise AuthError(
                        "invalid_status_transition",
                        f"Cannot move NCR from '{old_status}' to '{new_status}'.",
                        409,
                    )
                put("status", new_status)
                if new_status == "closed" and cur["documented_date"] is not None:
                    today = datetime.now(timezone.utc).date()
                    tat = (today - cur["documented_date"]).days
                    put("closure_tat_days", tat)

            # Apply only when there is something to change. The detail read
            # happens AFTER the transaction commits + the connection is released
            # (below), so it never blocks on its own FOR UPDATE lock.
            if sets:
                args.append(ncr_id)
                await conn.execute(
                    f"UPDATE ncr_record SET {', '.join(sets)} WHERE ncr_id = ${len(args)}",
                    *args,
                )

    return await get_detail(pool, ncr_id)


# ── Delete ─────────────────────────────────────────────────────────────────


async def delete(pool, ncr_id: int) -> None:
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM ncr_record WHERE ncr_id = $1", ncr_id)
    if result.endswith(" 0"):
        raise AuthError("ncr_not_found", f"NCR {ncr_id} not found.", 404)
