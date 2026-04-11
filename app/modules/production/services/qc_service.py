"""QC Inspection service — Section G1."""
from datetime import datetime, timezone


def _gen_id(seq_val: int) -> str:
    d = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"QCI-{d}-{seq_val:04d}"


# ── Get QC queue (all pending + recent inspections) ──
async def get_qc_queue(conn):
    rows = await conn.fetch("""
        SELECT qi.*, jc.job_card_number AS jc_number,
               jc.fg_sku_name, jc.customer_name, jc.floor,
               jc.batch_number, jc.batch_size_kg,
               po.best_before, po.batch_number AS po_batch_number,
               bh.shelf_life_days,
               jc.mrp, jc.control_sample_gm,
               jc.start_time AS production_start
        FROM qc_inspection qi
        JOIN job_card jc ON qi.job_card_id = jc.job_card_id
        LEFT JOIN production_order po ON jc.prod_order_id = po.prod_order_id
        LEFT JOIN bom_header bh ON jc.bom_id = bh.bom_id
        ORDER BY
            CASE WHEN qi.result = 'pending' THEN 0 ELSE 1 END,
            qi.created_at DESC
        LIMIT 200
    """)
    return {"results": [dict(r) for r in rows]}


# ── Submit / update inspection ──
async def submit_inspection(conn, inspection_id, *, result, findings=None,
                             corrective_action=None, inspector_user):
    now = datetime.now(timezone.utc)

    r = await conn.execute("""
        UPDATE qc_inspection
        SET result = $2, findings = $3, corrective_action = $4,
            inspector_user = $5, inspection_date = $6,
            signed_off_at = CASE WHEN $2 IN ('pass','conditional_pass') THEN $6 ELSE NULL END
        WHERE inspection_id = $1 OR id::text = $1
    """, str(inspection_id), result, findings, corrective_action, inspector_user, now)

    # If fail → update job card status to halt progression
    if result == "fail":
        row = await conn.fetchrow(
            "SELECT job_card_id FROM qc_inspection WHERE inspection_id = $1 OR id::text = $1",
            str(inspection_id),
        )
        if row:
            # Create alert for Production Planner and Store Manager
            try:
                await conn.execute("""
                    INSERT INTO production_alert (alert_type, severity, title, message,
                        related_entity_type, related_entity_id, entity)
                    VALUES ('qc_fail', 'high',
                            $1, $2, 'job_card', $3::text, 'cfpl')
                """, f"QC Fail: JC #{row['job_card_id']}",
                     f"QC inspection failed. Corrective action: {corrective_action or 'N/A'}",
                     row["job_card_id"])
            except Exception:
                pass  # Alert table may not exist yet

    return {"updated": r == "UPDATE 1", "result": result}


# ── Auto-create QC checkpoints when JC reaches a stage ──
async def create_checkpoints_for_jc(conn, job_card_id, jc_number=None,
                                     fg_sku_name=None, customer_name=None,
                                     floor=None):
    """Called when a job card is created or transitions — creates pending QC entries."""
    checkpoints = ["pre_production", "in_process", "post_production"]

    created = []
    for cp in checkpoints:
        # Check if already exists
        existing = await conn.fetchval("""
            SELECT inspection_id FROM qc_inspection
            WHERE job_card_id = $1 AND checkpoint_type = $2
        """, job_card_id, cp)
        if existing:
            continue

        seq = await conn.fetchval("SELECT nextval('seq_qci')")
        qci_id = _gen_id(seq)

        await conn.execute("""
            INSERT INTO qc_inspection
                (inspection_id, job_card_id, jc_number, fg_sku_name,
                 customer_name, floor, checkpoint_type)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
        """, qci_id, job_card_id, jc_number, fg_sku_name,
             customer_name, floor, cp)
        created.append(qci_id)

    return {"created": created}
