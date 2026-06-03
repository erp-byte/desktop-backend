"""Sample integrity check (checklist A11 / spec §9.9).

Verifies the cross-table invariants that the per-request transactions are meant
to uphold, and surfaces any drift as a store_alert (alert_type
'sample_integrity_drift', target_team 'stores'). Intended to run nightly via
scripts/sample_integrity_check.py; a clean dataset reports zero drift.
"""
from __future__ import annotations

from app.modules.sample.services import notification_service


async def run_integrity_check(conn, *, emit: bool = True) -> list[dict]:
    drifts: list[dict] = []

    # 1. GATE_PASS_ISSUED must have exactly one non-voided gate pass.
    rows = await conn.fetch(
        """
        SELECT sr.id, sr.requisition_number, sr.entity,
               (SELECT COUNT(*) FROM gate_passes gp
                 WHERE gp.source_ref_type = 'SAMPLE_REQ' AND gp.source_ref_id = sr.id
                   AND gp.voided = FALSE) AS gp_count
          FROM sample_requisitions sr
         WHERE sr.status = 'GATE_PASS_ISSUED' AND sr.deleted_at IS NULL
        """)
    for r in rows:
        if r["gp_count"] != 1:
            drifts.append(_d(r, "gate_pass_count", f"expected 1 non-voided gate pass, found {r['gp_count']}"))

    # 2. IN_PRODUCTION must link a non-REGULAR job card.
    rows = await conn.fetch(
        """
        SELECT sr.id, sr.requisition_number, sr.entity, sr.linked_job_card_id, jc.jobcard_type
          FROM sample_requisitions sr
          LEFT JOIN job_card jc ON jc.job_card_id = sr.linked_job_card_id
         WHERE sr.status = 'IN_PRODUCTION' AND sr.deleted_at IS NULL
           AND (sr.linked_job_card_id IS NULL OR jc.jobcard_type IS NULL OR jc.jobcard_type = 'REGULAR')
        """)
    for r in rows:
        drifts.append(_d(r, "jobcard_link", "IN_PRODUCTION without a non-REGULAR sample job card"))

    # 3. PARTIALLY_CONVERTED must have at least one child.
    rows = await conn.fetch(
        """
        SELECT sr.id, sr.requisition_number, sr.entity
          FROM sample_requisitions sr
         WHERE sr.status = 'PARTIALLY_CONVERTED' AND sr.deleted_at IS NULL
           AND NOT EXISTS (SELECT 1 FROM sample_requisitions c WHERE c.converted_from_id = sr.id)
        """)
    for r in rows:
        drifts.append(_d(r, "partial_child", "PARTIALLY_CONVERTED without a child requisition"))

    # 4. INTERNALLY_DISPATCHED must have exactly one 265 movement.
    rows = await conn.fetch(
        """
        SELECT sr.id, sr.requisition_number, sr.entity,
               (SELECT COUNT(*) FROM material_document md
                 WHERE md.sample_requisition_id = sr.id AND md.movement_type = '265') AS md_count
          FROM sample_requisitions sr
         WHERE sr.status = 'INTERNALLY_DISPATCHED' AND sr.deleted_at IS NULL
        """)
    for r in rows:
        if r["md_count"] != 1:
            drifts.append(_d(r, "movement_count", f"expected 1 movement (265), found {r['md_count']}"))

    if emit:
        for d in drifts:
            await notification_service.emit_alert(
                conn, alert_type="sample_integrity_drift",
                target_team=notification_service.TEAM_STORES,
                message=f"Sample integrity drift on {d['requisition_number']}: {d['check']} — {d['detail']}",
                related_id=d["requisition_id"], entity=d["entity"])
    return drifts


def _d(row, check: str, detail: str) -> dict:
    return {"requisition_id": row["id"], "requisition_number": row["requisition_number"],
            "entity": row["entity"], "check": check, "detail": detail}
