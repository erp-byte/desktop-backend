"""Sample notifications (checklist A11 / spec §11).

Thin wrappers over store_alert. `target_team` and `related_type` are free-text
columns on store_alert, so the sample vocabulary (business / inventory / npd,
sample_requisition / sample_gate_pass) needs no migration — only the consistent
constants below. Mirrors the direct-INSERT pattern used by discrepancy_manager.
"""
from __future__ import annotations

# target_team vocab (extends the existing purchase / stores / production / qc)
TEAM_BUSINESS   = "business"
TEAM_INVENTORY  = "inventory"
TEAM_PRODUCTION = "production"
TEAM_STORES     = "stores"
TEAM_NPD        = "npd"

# related_type vocab
REL_SAMPLE_REQ       = "sample_requisition"
REL_SAMPLE_GATE_PASS = "sample_gate_pass"


async def emit_alert(
    conn,
    *,
    alert_type: str,
    target_team: str,
    message: str,
    entity: str = "cfpl",   # store_alert is legal-entity scoped (cfpl/cdpl), NOT
                            # the sample warehouse; default to the CFPL entity.
    related_id: int | None = None,
    related_type: str = REL_SAMPLE_REQ,
) -> None:
    """Insert one store_alert row for a sample event."""
    await conn.execute(
        """
        INSERT INTO store_alert
            (alert_type, target_team, message, related_id, related_type, entity)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        alert_type, target_team, message, related_id, related_type, entity,
    )
