"""Sample requisition lifecycle (checklist A4 / spec §8).

Owns the requisition record + its article lines and the guarded status state
machine. Approvals, outward movements, job cards, gate passes and conversions
live in sibling services and call transition_status() here to move the record.

Numbering follows the house _gen_id pattern (PREFIX-YYYYMMDD-NNNN via a
dedicated sequence) — see material_document_service._gen_id.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException

from app.modules.sample.services import audit_service, notification_service

WAREHOUSES = ("W202", "A185", "A68", "F53", "A101", "D-39", "D-514", "Rishi", "Supreme")

SAMPLE_TYPES = ("BASIS_RM", "BASIS_FG", "NPD", "INTERNAL", "TRIAL")

# Status state machine (spec §8). Maps current -> set of allowed next states.
TRANSITIONS: dict[str, set[str]] = {
    "DRAFT":                 {"SUBMITTED", "CANCELLED"},
    "SUBMITTED":             {"BH_APPROVED", "BH_REJECTED", "CANCELLED"},
    "BH_REJECTED":           {"SUBMITTED", "CANCELLED"},
    "BH_APPROVED":           {"IN_PRODUCTION", "READY_FOR_DISPATCH", "CANCELLED"},
    "IN_PRODUCTION":         {"PACKING", "CANCELLED"},
    "PACKING":               {"READY_FOR_DISPATCH", "CANCELLED"},
    "READY_FOR_DISPATCH":    {"GATE_PASS_ISSUED", "INTERNALLY_DISPATCHED", "CANCELLED"},
    "GATE_PASS_ISSUED":      {"CLOSED"},
    "INTERNALLY_DISPATCHED": {"GATE_PASS_ISSUED", "PARTIALLY_CONVERTED", "CLOSED"},
    "PARTIALLY_CONVERTED":   {"GATE_PASS_ISSUED", "CLOSED"},
    "CLOSED":                set(),
    "CANCELLED":             set(),
}


def _gen_requisition_number(seq_val: int) -> str:
    d = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"SMP-{d}-{seq_val:04d}"


def _assert_transition(current: str, target: str) -> None:
    """Raise 409 if current -> target is not an allowed status transition."""
    if target not in TRANSITIONS.get(current, set()):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "illegal_transition",
                "message": f"Cannot move requisition from {current} to {target}",
                "details": {"current": current, "target": target},
            },
        )


async def _fetch_req(conn, req_id: int) -> dict | None:
    row = await conn.fetchrow(
        "SELECT * FROM sample_requisitions WHERE id = $1 AND deleted_at IS NULL",
        req_id,
    )
    return dict(row) if row else None


def _require(req: dict | None, req_id: int) -> dict:
    if req is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found",
                    "message": f"Sample requisition {req_id} not found",
                    "details": {"id": req_id}},
        )
    return req


# ---------------------------------------------------------------------------
# Create / read / list
# ---------------------------------------------------------------------------
async def create_requisition(conn, *, payload: dict, user) -> dict:
    """Insert a DRAFT requisition + its article lines. One transaction."""
    sample_type = payload["sample_type"]
    if sample_type not in SAMPLE_TYPES:
        raise HTTPException(422, detail={"error": "invalid_sample_type",
                                         "message": f"sample_type must be one of {SAMPLE_TYPES}",
                                         "details": {"sample_type": sample_type}})
    warehouse = payload.get("warehouse")
    if warehouse not in WAREHOUSES:
        raise HTTPException(422, detail={"error": "invalid_warehouse",
                                         "message": f"warehouse must be one of {WAREHOUSES}",
                                         "details": {"warehouse": warehouse}})

    articles = payload.get("articles") or []

    async with conn.transaction():
        seq = await conn.fetchval("SELECT nextval('seq_sample_req')")
        number = _gen_requisition_number(seq)
        req_id = await conn.fetchval(
            """
            INSERT INTO sample_requisitions
                (requisition_number, sample_type, status, requestor_user_id,
                 requestor_team, purpose_tag, purpose_note, base_bom_id,
                 internal_override, warehouse, transporter_name, vehicle_number,
                 npd_target_name, quantity, created_by, updated_by)
            VALUES ($1, $2, 'DRAFT', $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $3, $3)
            RETURNING id
            """,
            number, sample_type, user.user_id, payload.get("requestor_team"),
            payload.get("purpose_tag"), payload.get("purpose_note"),
            payload.get("base_bom_id"),
            bool(payload.get("internal_override", False)), warehouse,
            payload.get("transporter_name"), payload.get("vehicle_number"),
            payload.get("npd_target_name"), payload.get("quantity"),
        )
        await _insert_articles(conn, req_id, articles)
        await audit_service.write_audit(
            conn, req_id, audit_service.EV_STATUS_CHANGE,
            new_value={"status": "DRAFT", "requisition_number": number},
            actor_user_id=user.user_id, actor_role=user.role_name,
            remarks="Requisition created",
        )
    return await get_requisition(conn, req_id)


async def _insert_articles(conn, req_id: int, articles: list[dict]) -> None:
    for a in articles:
        if not a.get("sku_id"):
            # Free-text articles are rejected at the boundary (spec §15.1) —
            # a real sku_id from /api/v1/so/sku-lookup is mandatory.
            raise HTTPException(422, detail={
                "error": "invalid_article",
                "message": "Each article must carry a sku_id from the SKU lookup",
                "details": {"article": a}})
        if float(a.get("required_qty") or 0) <= 0:
            raise HTTPException(422, detail={
                "error": "invalid_qty",
                "message": "required_qty must be > 0",
                "details": {"article": a}})
        await conn.execute(
            """
            INSERT INTO sample_requisition_articles
                (requisition_id, sku_id, sku_name, required_qty, uom,
                 article_role, pack_size_kg, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            req_id, a["sku_id"], a["sku_name"], a["required_qty"],
            a["uom"], a["article_role"], a.get("pack_size_kg"), a.get("notes"),
        )


async def get_requisition(conn, req_id: int) -> dict:
    req = _require(await _fetch_req(conn, req_id), req_id)
    articles = await conn.fetch(
        "SELECT * FROM sample_requisition_articles WHERE requisition_id = $1 ORDER BY id",
        req_id)
    approvals = await conn.fetch(
        "SELECT * FROM sample_approvals WHERE requisition_id = $1 ORDER BY sequence_no",
        req_id)
    audit = await conn.fetch(
        "SELECT * FROM sample_audit_log WHERE requisition_id = $1 ORDER BY created_at",
        req_id)
    req["articles"] = [dict(r) for r in articles]
    req["approvals"] = [dict(r) for r in approvals]
    req["audit"] = [dict(r) for r in audit]
    return req


async def list_requisitions(conn, *, status: str | None = None,
                            sample_type: str | None = None,
                            warehouse: str | None = None,
                            limit: int = 50, offset: int = 0) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT * FROM sample_requisitions
        WHERE deleted_at IS NULL
          AND ($1::text IS NULL OR status = $1)
          AND ($2::text IS NULL OR sample_type = $2)
          AND ($3::text IS NULL OR warehouse = $3)
        ORDER BY created_at DESC
        LIMIT $4 OFFSET $5
        """,
        status, sample_type, warehouse, limit, offset)
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------
async def update_requisition(conn, req_id: int, *, payload: dict, user) -> dict:
    """Edit a DRAFT or BH_REJECTED requisition (header + article replacement)."""
    req = _require(await _fetch_req(conn, req_id), req_id)
    if req["status"] not in ("DRAFT", "BH_REJECTED"):
        raise HTTPException(409, detail={
            "error": "not_editable",
            "message": "Only DRAFT or BH_REJECTED requisitions can be edited",
            "details": {"status": req["status"]}})
    async with conn.transaction():
        await conn.execute(
            """
            UPDATE sample_requisitions
               SET requestor_team   = COALESCE($2, requestor_team),
                   purpose_tag      = COALESCE($3, purpose_tag),
                   purpose_note     = COALESCE($4, purpose_note),
                   base_bom_id      = COALESCE($5, base_bom_id),
                   transporter_name = COALESCE($7, transporter_name),
                   vehicle_number   = COALESCE($8, vehicle_number),
                   quantity         = COALESCE($9, quantity),
                   updated_at = NOW(), updated_by = $6
             WHERE id = $1
            """,
            req_id, payload.get("requestor_team"), payload.get("purpose_tag"),
            payload.get("purpose_note"), payload.get("base_bom_id"), user.user_id,
            payload.get("transporter_name"), payload.get("vehicle_number"),
            payload.get("quantity"))
        if payload.get("articles") is not None:
            await conn.execute(
                "DELETE FROM sample_requisition_articles WHERE requisition_id = $1", req_id)
            await _insert_articles(conn, req_id, payload["articles"])
        await audit_service.write_audit(
            conn, req_id, audit_service.EV_ARTICLE_EDIT,
            actor_user_id=user.user_id, actor_role=user.role_name,
            remarks="Requisition edited")
    return await get_requisition(conn, req_id)


async def submit_requisition(conn, req_id: int, *, user) -> dict:
    """DRAFT|BH_REJECTED -> SUBMITTED with validation guards (spec §8)."""
    req = _require(await _fetch_req(conn, req_id), req_id)
    _assert_transition(req["status"], "SUBMITTED")

    # NPD / TRIAL requests are a pure ask — they name the target article only;
    # the NPD team authors the recipe (articles/BOM) later. Article lines are
    # therefore required only for the issuance flows (Basis RM/FG, Internal).
    if req["sample_type"] not in ("NPD", "TRIAL"):
        n_articles = await conn.fetchval(
            "SELECT COUNT(*) FROM sample_requisition_articles WHERE requisition_id = $1", req_id)
        if not n_articles:
            raise HTTPException(422, detail={
                "error": "no_articles",
                "message": "A requisition needs at least one article before submission",
                "details": {"id": req_id}})

    async with conn.transaction():
        await conn.execute(
            "UPDATE sample_requisitions SET status='SUBMITTED', updated_at=NOW(), updated_by=$2 WHERE id=$1",
            req_id, user.user_id)
        await audit_service.write_audit(
            conn, req_id, audit_service.EV_STATUS_CHANGE,
            old_value={"status": req["status"]}, new_value={"status": "SUBMITTED"},
            actor_user_id=user.user_id, actor_role=user.role_name,
            remarks="Submitted for BH approval")
        # Path A handoff: a raised NPD / TRIAL request is the business team asking
        # NPD to develop an article — alert the NPD team so they can pick it up
        # (recipe authoring is allowed pre-approval; promotion still needs the BH
        # gate). Other sample types route through the approval-stage alerts only.
        if req["sample_type"] in ("NPD", "TRIAL"):
            tgt = req.get("npd_target_name")
            await notification_service.emit_alert(
                conn, alert_type="sample_npd_requested",
                target_team=notification_service.TEAM_NPD,
                message=(f"New {req['sample_type']} request {req['requisition_number']} "
                         f"raised for development" + (f": {tgt}." if tgt else ".")),
                related_id=req_id)
    return await get_requisition(conn, req_id)


async def cancel_requisition(conn, req_id: int, *, reason: str, user) -> dict:
    """Any non-terminal status -> CANCELLED (requires a reason, spec §8)."""
    req = _require(await _fetch_req(conn, req_id), req_id)
    _assert_transition(req["status"], "CANCELLED")
    if not (reason or "").strip():
        raise HTTPException(422, detail={
            "error": "reason_required",
            "message": "cancellation_reason is required",
            "details": {"id": req_id}})
    async with conn.transaction():
        await conn.execute(
            """UPDATE sample_requisitions
                  SET status='CANCELLED', cancellation_reason=$2,
                      updated_at=NOW(), updated_by=$3
                WHERE id=$1""",
            req_id, reason, user.user_id)
        await audit_service.write_audit(
            conn, req_id, audit_service.EV_CANCEL,
            old_value={"status": req["status"]}, new_value={"status": "CANCELLED"},
            actor_user_id=user.user_id, actor_role=user.role_name, remarks=reason)
    return await get_requisition(conn, req_id)


async def transition_status(conn, req_id: int, *, target: str, user,
                            remarks: str | None = None,
                            extra: dict | None = None) -> dict:
    """Guarded status move used by sibling services (approval/outward/etc.).

    `extra` is an optional dict of additional column=value updates applied in
    the same statement (e.g. {"linked_job_card_id": 42}).
    """
    req = _require(await _fetch_req(conn, req_id), req_id)
    _assert_transition(req["status"], target)
    sets = ["status = $2", "updated_at = NOW()", "updated_by = $3"]
    params: list = [req_id, target, user.user_id]
    for col, val in (extra or {}).items():
        params.append(val)
        sets.append(f"{col} = ${len(params)}")
    await conn.execute(
        f"UPDATE sample_requisitions SET {', '.join(sets)} WHERE id = $1", *params)
    await audit_service.write_audit(
        conn, req_id, audit_service.EV_STATUS_CHANGE,
        old_value={"status": req["status"]}, new_value={"status": target},
        actor_user_id=user.user_id, actor_role=user.role_name, remarks=remarks)
    return _require(await _fetch_req(conn, req_id), req_id)


async def close_requisition(conn, req_id: int, *, user, remarks: str | None = None) -> dict:
    """Close a dispatched/issued requisition (-> CLOSED). Allowed from
    GATE_PASS_ISSUED, INTERNALLY_DISPATCHED, PARTIALLY_CONVERTED (spec §8)."""
    req = _require(await _fetch_req(conn, req_id), req_id)
    async with conn.transaction():
        await transition_status(conn, req_id, target="CLOSED", user=user,
                                remarks=remarks or "Closed")
    return await get_requisition(conn, req_id)
