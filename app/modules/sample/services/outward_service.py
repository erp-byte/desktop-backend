"""Sample outward movements for Basis RM / Internal (checklist A6 / spec §6, §9.1).

Reuses the canonical SAP-style writer create_material_document() — no parallel
outward table. Every sample movement is movement_type 265 + reference_type
SAMPLE_REQ, so it is uniquely filterable in stock-movement reports without
polluting 261 (GI-to-Production).
"""
from __future__ import annotations

from fastapi import HTTPException

from app.modules.production.services.material_document_service import (
    create_material_document, MVT_GI_SAMPLE, REF_SAMPLE_REQ,
)
from app.modules.sample.services import audit_service, notification_service
from app.modules.sample.services import requisition_service as req_svc


async def issue_outward(conn, req_id: int, *, user,
                        from_location: str | None = None,
                        issued: dict[int, float] | None = None) -> dict:
    """BH_APPROVED -> READY_FOR_DISPATCH: book the material_document (265).

    `issued` optionally maps article id -> issued_qty; defaults to required_qty.
    One transaction: material doc + lines + article issued_qty + status + audit.
    """
    issued = issued or {}
    async with conn.transaction():
        # Lock the requisition row + re-assert status INSIDE the txn so two
        # concurrent issuers can't both pass the BH_APPROVED check and book a
        # second 265 movement (double deduction — spec §9.3).
        locked = await conn.fetchrow(
            "SELECT * FROM sample_requisitions WHERE id = $1 AND deleted_at IS NULL FOR UPDATE", req_id)
        if locked is None:
            raise HTTPException(404, detail={"error": "not_found",
                                             "message": f"Sample requisition {req_id} not found",
                                             "details": {"id": req_id}})
        req = dict(locked)
        if req["sample_type"] not in ("BASIS_RM", "INTERNAL"):
            raise HTTPException(409, detail={
                "error": "wrong_flow",
                "message": "Direct outward applies only to BASIS_RM / INTERNAL samples",
                "details": {"sample_type": req["sample_type"]}})
        if req["status"] != "BH_APPROVED":
            raise HTTPException(409, detail={
                "error": "not_approved",
                "message": "Requisition must be BH_APPROVED before outward",
                "details": {"status": req["status"]}})

        articles = await conn.fetch(
            "SELECT * FROM sample_requisition_articles WHERE requisition_id = $1 ORDER BY id", req_id)
        if not articles:
            raise HTTPException(422, detail={"error": "no_articles",
                                             "message": "No article lines to issue",
                                             "details": {"id": req_id}})

        lines = []
        for a in articles:
            qty = float(issued.get(a["id"], a["required_qty"]))
            lines.append({
                "sku_name": a["sku_name"],
                "quantity_kg": qty,
                "uom": a["uom"],
                "from_location": from_location,
                "to_location": "SAMPLE",
            })

        # material_document is legal-entity scoped (cfpl/cdpl), not by warehouse;
        # create_material_document defaults entity='cfpl'.
        doc = await create_material_document(
            conn, movement_type=MVT_GI_SAMPLE, reference_type=REF_SAMPLE_REQ,
            reference_id=str(req_id), created_by=user.full_name or str(user.user_id),
            lines=lines,
            notes=f"Sample outward for {req['request_id']}")
        # Stamp the new sample linkage column (create_material_document is generic)
        await conn.execute(
            "UPDATE material_document SET sample_requisition_id = $1 WHERE mat_doc_id = $2",
            req_id, doc["mat_doc_id"])
        for a in articles:
            qty = float(issued.get(a["id"], a["required_qty"]))
            await conn.execute(
                "UPDATE sample_requisition_articles SET issued_qty = $1 WHERE id = $2",
                qty, a["id"])
        await audit_service.write_audit(
            conn, req_id, audit_service.EV_OUTWARD,
            new_value={"mat_doc_id": doc["mat_doc_id"], "movement_type": MVT_GI_SAMPLE},
            actor_user_id=user.user_id, actor_role=user.role_name,
            remarks="Sample material issued (265)")
        await req_svc.transition_status(
            conn, req_id, target="READY_FOR_DISPATCH", user=user,
            remarks="Material issued, ready for dispatch")
        await notification_service.emit_alert(
            conn, alert_type="sample_rm_outward_done",
            target_team=notification_service.TEAM_INVENTORY,
            message=f"Sample {req['request_id']} material issued (265).",
            related_id=req_id)

    return await req_svc.get_requisition(conn, req_id)


async def dispatch_internal(conn, req_id: int, *, user) -> dict:
    """READY_FOR_DISPATCH -> INTERNALLY_DISPATCHED (internal flow, no gate pass)."""
    req = req_svc._require(await req_svc._fetch_req(conn, req_id), req_id)
    if req["sample_type"] != "INTERNAL":
        raise HTTPException(409, detail={
            "error": "wrong_flow",
            "message": "Internal dispatch applies only to INTERNAL samples",
            "details": {"sample_type": req["sample_type"]}})
    async with conn.transaction():
        await req_svc.transition_status(
            conn, req_id, target="INTERNALLY_DISPATCHED", user=user,
            remarks="Dispatched internally")
        await notification_service.emit_alert(
            conn, alert_type="sample_internally_dispatched",
            target_team=notification_service.TEAM_INVENTORY,
            message=f"Sample {req['request_id']} dispatched internally.",
            related_id=req_id)
    return await req_svc.get_requisition(conn, req_id)
