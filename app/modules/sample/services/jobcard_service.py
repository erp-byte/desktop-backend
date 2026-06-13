"""Sample FG / NPD job-card integration (checklist A7 / spec §8).

BASIS_FG and NPD samples run through the standard production engine: a sample
production_order is created (no plan line) and create_job_cards() generates the
chained job cards, stamped with sample_requisition_id + jobcard_type so they are
traceable and excludable from regular reports. The requisition then reacts to
the job-card lifecycle (IN_PRODUCTION -> PACKING -> READY_FOR_DISPATCH).
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import HTTPException

from app.modules.production.services.job_card_engine import create_job_cards
from app.modules.sample.services import audit_service, notification_service
from app.modules.sample.services import requisition_service as req_svc

JOBCARD_TYPE = {"BASIS_FG": "BASIS_FG_SAMPLE", "NPD": "NPD_SAMPLE", "TRIAL": "TRIAL_SAMPLE"}


async def _create_sample_production_order(conn, req: dict, bom_id: int,
                                          batch_size_kg: float) -> dict:
    """Insert a plan-less production_order for a sample run; mirrors the
    numbering + columns of job_card_engine.create_production_orders."""
    bom = await conn.fetchrow("SELECT * FROM bom_header WHERE bom_id = $1", bom_id)
    if not bom:
        raise HTTPException(422, detail={"error": "bom_not_found",
                                         "message": f"BOM {bom_id} not found",
                                         "details": {"bom_id": bom_id}})
    year = date.today().year
    seq = await conn.fetchval(
        "SELECT COALESCE(MAX(CAST(SUBSTRING(prod_order_number FROM 10) AS INT)), 0) + 1 "
        "FROM production_order WHERE prod_order_number LIKE $1",
        f"PRD-{year}-%")
    prod_order_number = f"PRD-{year}-{seq:04d}"
    batch_number = f"B{year}-{seq:04d}"
    total_stages = max(await conn.fetchval(
        "SELECT COUNT(*) FROM bom_process_route WHERE bom_id = $1", bom_id) or 1, 1)
    shelf_life = bom.get("shelf_life_days") or 180
    floors = bom.get("floors") or []
    prod_order_id = await conn.fetchval(
        """
        INSERT INTO production_order (
            prod_order_number, plan_line_id, bom_id, fg_sku_name, customer_name,
            batch_number, batch_size_kg, net_wt_per_unit, best_before,
            total_stages, entity, factory, floor, status
        ) VALUES ($1, NULL, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, 'created')
        RETURNING prod_order_id
        """,
        prod_order_number, bom_id, bom["fg_sku_name"], bom.get("customer_name"),
        batch_number, batch_size_kg, float(bom.get("pack_size_kg") or 0),
        # production_order is legal-entity scoped (cfpl/cdpl), not by warehouse.
        date.today() + timedelta(days=shelf_life), total_stages, "cfpl",
        bom.get("factory") or "W202", floors[0] if floors else None)
    return {"prod_order_id": prod_order_id, "prod_order_number": prod_order_number}


async def start_production(conn, req_id: int, *, user) -> dict:
    """BH_APPROVED -> IN_PRODUCTION: create sample production order + job cards."""
    req = req_svc._require(await req_svc._fetch_req(conn, req_id), req_id)
    if req["sample_type"] not in ("BASIS_FG", "NPD", "TRIAL"):
        raise HTTPException(409, detail={"error": "wrong_flow",
                                         "message": "Job cards apply only to BASIS_FG / NPD / TRIAL samples",
                                         "details": {"sample_type": req["sample_type"]}})
    if req["status"] != "BH_APPROVED":
        raise HTTPException(409, detail={"error": "not_approved",
                                         "message": "Requisition must be BH_APPROVED",
                                         "details": {"status": req["status"]}})
    bom_id = req["base_bom_id"]
    if not bom_id:
        raise HTTPException(422, detail={
            "error": "bom_required",
            "message": "base_bom_id is required to generate sample job cards",
            "details": {"id": req_id}})

    # Batch size = sum of FG / NPD_OUTPUT article quantities; if a requisition
    # carries only input articles, fall back to 1.0 so a job card can still run.
    batch_size = await conn.fetchval(
        """SELECT COALESCE(SUM(required_qty), 0) FROM sample_requisition_articles
            WHERE requisition_id = $1 AND article_role IN ('FG','NPD_OUTPUT')""", req_id)
    batch_size = float(batch_size or 0) or 1.0
    jc_type = JOBCARD_TYPE[req["sample_type"]]

    async with conn.transaction():
        po = await _create_sample_production_order(conn, req, bom_id, batch_size)
        result = await create_job_cards(
            conn, po["prod_order_id"], sample_requisition_id=req_id, jobcard_type=jc_type)
        jcs = result.get("job_cards") or []
        last_jc = jcs[-1]["job_card_id"] if jcs else None
        await req_svc.transition_status(
            conn, req_id, target="IN_PRODUCTION", user=user,
            remarks=f"Sample job cards generated ({po['prod_order_number']})",
            extra={"linked_job_card_id": last_jc})
        await audit_service.write_audit(
            conn, req_id, audit_service.EV_JOBCARD,
            new_value={"prod_order": po["prod_order_number"],
                       "job_cards": len(jcs), "jobcard_type": jc_type},
            actor_user_id=user.user_id, actor_role=user.role_name,
            remarks="Job cards created")
        await notification_service.emit_alert(
            conn, alert_type="sample_jobcard_created",
            target_team=notification_service.TEAM_PRODUCTION,
            message=f"Sample {req['request_id']} job cards issued ({po['prod_order_number']}).",
            related_id=req_id)
    return await req_svc.get_requisition(conn, req_id)


async def mark_packing(conn, req_id: int, *, user) -> dict:
    """IN_PRODUCTION -> PACKING (production complete on the sample chain)."""
    req = req_svc._require(await req_svc._fetch_req(conn, req_id), req_id)
    async with conn.transaction():
        await req_svc.transition_status(conn, req_id, target="PACKING", user=user,
                                        remarks="Production complete, packing")
        await notification_service.emit_alert(
            conn, alert_type="sample_jobcard_completed",
            target_team=notification_service.TEAM_INVENTORY,
            message=f"Sample {req['request_id']} production complete — packing.",
            related_id=req_id)
    return await req_svc.get_requisition(conn, req_id)


async def mark_ready(conn, req_id: int, *, user) -> dict:
    """PACKING -> READY_FOR_DISPATCH."""
    req = req_svc._require(await req_svc._fetch_req(conn, req_id), req_id)
    async with conn.transaction():
        await req_svc.transition_status(conn, req_id, target="READY_FOR_DISPATCH",
                                        user=user, remarks="Packed, ready for dispatch")
    return await req_svc.get_requisition(conn, req_id)
