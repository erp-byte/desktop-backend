"""Webhook Event Registry — every event defined once with its metadata.

Each function is a self-contained event "tool":
  - Name = event_type (dot-separated)
  - target_roles = who gets WebSocket push
  - Payload = explicitly typed kwargs, not a raw dict

Services call:
    from app.webhooks import events
    await events.indent_sent(entity, indent_id=42, material="Sugar", qty_kg=500.0)

No need to know about Event, event_bus, or target_roles.
"""

import logging

from .event_bus import event_bus, Event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers (H2/H3/H4/L4)
# ---------------------------------------------------------------------------

_ALLOWED_ENTITIES = {"cfpl", "cdpl", ""}


def _validate_entity(entity, event_type: str) -> str:
    """Normalize + validate the entity label for an event emit.

    - Coerces None -> "" (broadcast / cross-entity).
    - Strips + lowercases string entities.
    - Warns (but does NOT raise) if the entity is unrecognised, so a malformed
      caller cannot break the business flow — emit proceeds with the
      normalized value.
    """
    if entity is None:
        return ""
    if not isinstance(entity, str):
        logger.warning(
            "event %s: entity has non-string type %s; coercing to string",
            event_type, type(entity).__name__,
        )
        entity = str(entity)
    norm = entity.strip().lower()
    if norm not in _ALLOWED_ENTITIES:
        logger.warning(
            "event %s: unexpected entity %r (allowed: %s)",
            event_type, entity, sorted(e for e in _ALLOWED_ENTITIES if e),
        )
    return norm


# ---------------------------------------------------------------------------
# Fulfillment
# ---------------------------------------------------------------------------

async def fulfillment_synced(entity: str, *, synced: int, skipped: int, total: int) -> None:
    entity = _validate_entity(entity, "fulfillment.synced")
    await event_bus.publish(Event(
        event_type="fulfillment.synced",
        entity=entity,
        payload={"synced": synced, "skipped": skipped, "total": total},
        target_roles=["planner", "admin"],
    ))


async def fulfillment_revised(entity: str, *, fulfillment_id: int,
                               new_qty: float | None = None,
                               new_date: str | None = None,
                               revised_by: str = "") -> None:
    entity = _validate_entity(entity, "fulfillment.revised")
    await event_bus.publish(Event(
        event_type="fulfillment.revised",
        entity=entity,
        payload={"fulfillment_id": fulfillment_id, "new_qty": new_qty,
                 "new_date": new_date, "revised_by": revised_by},
        target_roles=["planner", "admin"],
    ))


# ---------------------------------------------------------------------------
# Production Plans
# ---------------------------------------------------------------------------

async def plan_approved(entity: str, *, plan_id: int, approved_by: str,
                        mrp_summary: dict | None = None) -> None:
    entity = _validate_entity(entity, "plan.approved")
    await event_bus.publish(Event(
        event_type="plan.approved",
        entity=entity,
        payload={"plan_id": plan_id, "approved_by": approved_by,
                 **({"mrp_summary": mrp_summary} if mrp_summary else {})},
        target_roles=["planner", "admin"],
    ))


# ---------------------------------------------------------------------------
# MRP
# ---------------------------------------------------------------------------

async def mrp_completed(entity: str, *, plan_id: int, summary: dict) -> None:
    entity = _validate_entity(entity, "mrp.completed")
    await event_bus.publish(Event(
        event_type="mrp.completed",
        entity=entity,
        payload={"plan_id": plan_id, "summary": summary},
        target_roles=["planner", "admin"],
    ))


async def mrp_shortage_detected(entity: str, *, plan_id: int,
                                 shortage_count: int,
                                 total_shortage_kg: float) -> None:
    entity = _validate_entity(entity, "mrp.shortage_detected")
    await event_bus.publish(Event(
        event_type="mrp.shortage_detected",
        entity=entity,
        payload={"plan_id": plan_id, "shortage_count": shortage_count,
                 "total_shortage_kg": total_shortage_kg},
        target_roles=["planner", "store_manager", "admin"],
    ))


# ---------------------------------------------------------------------------
# Indents
# ---------------------------------------------------------------------------

async def indent_drafted(entity: str, *, plan_id: int, count: int,
                         total_shortage_kg: float) -> None:
    entity = _validate_entity(entity, "indent.drafted")
    await event_bus.publish(Event(
        event_type="indent.drafted",
        entity=entity,
        payload={"plan_id": plan_id, "count": count,
                 "total_shortage_kg": total_shortage_kg},
        target_roles=["planner", "admin"],
    ))


async def indent_sent(entity: str, *, indent_id: int, material: str,
                      qty_kg: float, deadline: str | None = None) -> None:
    entity = _validate_entity(entity, "indent.sent")
    await event_bus.publish(Event(
        event_type="indent.sent",
        entity=entity,
        payload={"indent_id": indent_id, "material": material,
                 "qty_kg": qty_kg, "deadline": deadline},
        target_roles=["store_manager", "purchase", "admin"],
    ))


async def indent_bulk_sent(entity: str, *, indent_ids: list[int],
                           sent: int) -> None:
    entity = _validate_entity(entity, "indent.bulk_sent")
    await event_bus.publish(Event(
        event_type="indent.bulk_sent",
        entity=entity,
        payload={"indent_ids": indent_ids, "sent": sent},
        target_roles=["store_manager", "purchase", "admin"],
    ))


# ---------------------------------------------------------------------------
# Job Cards
# ---------------------------------------------------------------------------

async def job_card_created(entity: str, *, prod_order_id: int,
                           job_card_count: int) -> None:
    entity = _validate_entity(entity, "job_card.created")
    await event_bus.publish(Event(
        event_type="job_card.created",
        entity=entity,
        payload={"prod_order_id": prod_order_id,
                 "job_card_count": job_card_count},
        target_roles=["floor_supervisor", "admin"],
    ))


async def job_card_started(entity: str, *, job_card_id: int,
                           job_card_number: str, fg_sku_name: str,
                           floor: str | None = None) -> None:
    entity = _validate_entity(entity, "job_card.started")
    await event_bus.publish(Event(
        event_type="job_card.started",
        entity=entity,
        payload={"job_card_id": job_card_id, "job_card_number": job_card_number,
                 "fg_sku_name": fg_sku_name, "floor": floor},
        target_roles=["floor_supervisor", "admin"],
    ))


async def job_card_completed(entity: str, *, job_card_id: int,
                             job_card_number: str, fg_sku_name: str,
                             duration_minutes: float | None = None) -> None:
    entity = _validate_entity(entity, "job_card.completed")
    await event_bus.publish(Event(
        event_type="job_card.completed",
        entity=entity,
        payload={"job_card_id": job_card_id, "job_card_number": job_card_number,
                 "fg_sku_name": fg_sku_name, "duration_minutes": duration_minutes},
        target_roles=["floor_supervisor", "planner", "admin"],
    ))


async def job_card_team_assigned(entity: str, *, job_card_id: int,
                                  job_card_number: str, team_leader: str,
                                  member_count: int) -> None:
    entity = _validate_entity(entity, "job_card.team_assigned")
    await event_bus.publish(Event(
        event_type="job_card.team_assigned",
        entity=entity,
        payload={"job_card_id": job_card_id, "job_card_number": job_card_number,
                 "team_leader": team_leader, "member_count": member_count},
        target_roles=["floor_supervisor", "admin"],
    ))


async def job_card_material_received(entity: str, *, job_card_id: int,
                                      job_card_number: str, boxes_scanned: int,
                                      total_kg: float) -> None:
    entity = _validate_entity(entity, "job_card.material_received")
    await event_bus.publish(Event(
        event_type="job_card.material_received",
        entity=entity,
        payload={"job_card_id": job_card_id, "job_card_number": job_card_number,
                 "boxes_scanned": boxes_scanned, "total_kg": total_kg},
        target_roles=["floor_supervisor", "store_manager", "admin"],
    ))


async def job_card_material_acknowledged(entity: str, *, job_card_id: int,
                                          job_card_number: str) -> None:
    entity = _validate_entity(entity, "job_card.material_acknowledged")
    await event_bus.publish(Event(
        event_type="job_card.material_acknowledged",
        entity=entity,
        payload={"job_card_id": job_card_id, "job_card_number": job_card_number},
        target_roles=["floor_supervisor", "store_manager", "admin"],
    ))


async def job_card_dispatched_to_next(entity: str, *, job_card_id: int,
                                       job_card_number: str, qty_kg: float,
                                       dispatched_by: str) -> None:
    entity = _validate_entity(entity, "job_card.dispatched_to_next")
    await event_bus.publish(Event(
        event_type="job_card.dispatched_to_next",
        entity=entity,
        payload={"job_card_id": job_card_id, "job_card_number": job_card_number,
                 "qty_kg": qty_kg, "dispatched_by": dispatched_by},
        target_roles=["floor_supervisor", "admin"],
    ))


async def job_card_output_saved(entity: str, *, job_card_id: int,
                                 job_card_number: str, fg_actual_kg: float,
                                 yield_pct: float | None = None) -> None:
    entity = _validate_entity(entity, "job_card.output_saved")
    await event_bus.publish(Event(
        event_type="job_card.output_saved",
        entity=entity,
        payload={"job_card_id": job_card_id, "job_card_number": job_card_number,
                 "fg_actual_kg": fg_actual_kg, "yield_pct": yield_pct},
        target_roles=["floor_supervisor", "planner", "admin"],
    ))


async def job_card_signed_off(entity: str, *, job_card_id: int,
                               job_card_number: str, sign_off_type: str,
                               signed_by: str) -> None:
    entity = _validate_entity(entity, "job_card.signed_off")
    await event_bus.publish(Event(
        event_type="job_card.signed_off",
        entity=entity,
        payload={"job_card_id": job_card_id, "job_card_number": job_card_number,
                 "sign_off_type": sign_off_type, "signed_by": signed_by},
        target_roles=["floor_supervisor", "admin"],
    ))


async def job_card_force_unlocked(entity: str, *, job_card_id: int,
                                   job_card_number: str,
                                   reason: str) -> None:
    entity = _validate_entity(entity, "job_card.force_unlocked")
    await event_bus.publish(Event(
        event_type="job_card.force_unlocked",
        entity=entity,
        payload={"job_card_id": job_card_id, "job_card_number": job_card_number,
                 "reason": reason},
        target_roles=["floor_supervisor", "admin"],
    ))


async def indent_raised(entity: str, *, indent_id: int, material: str,
                        qty_kg: float, source: str = "manual",
                        job_card_id: int | None = None) -> None:
    entity = _validate_entity(entity, "indent.raised")
    await event_bus.publish(Event(
        event_type="indent.raised",
        entity=entity,
        payload={"indent_id": indent_id, "material": material,
                 "qty_kg": qty_kg, "source": source,
                 "job_card_id": job_card_id},
        target_roles=["store_manager", "purchase", "admin"],
    ))


# ---------------------------------------------------------------------------
# QC
# ---------------------------------------------------------------------------

async def qc_passed(entity: str, *, inspection_id: str, findings: str | None = None) -> None:
    entity = _validate_entity(entity, "qc.passed")
    await event_bus.publish(Event(
        event_type="qc.passed",
        entity=entity,
        payload={"inspection_id": inspection_id, "result": "pass",
                 "findings": findings},
        target_roles=["floor_supervisor", "admin"],
    ))


async def qc_failed(entity: str, *, inspection_id: str, findings: str | None = None) -> None:
    entity = _validate_entity(entity, "qc.failed")
    await event_bus.publish(Event(
        event_type="qc.failed",
        entity=entity,
        payload={"inspection_id": inspection_id, "result": "fail",
                 "findings": findings},
        target_roles=["floor_supervisor", "admin"],
    ))


# ---------------------------------------------------------------------------
# Material Movement
# ---------------------------------------------------------------------------

async def material_moved(entity: str, *, sku_name: str, from_location: str,
                         to_location: str, qty_kg: float,
                         movement_id: int | None = None) -> None:
    entity = _validate_entity(entity, "material.moved")
    await event_bus.publish(Event(
        event_type="material.moved",
        entity=entity,
        payload={"sku_name": sku_name, "from": from_location,
                 "to": to_location, "qty_kg": qty_kg,
                 "movement_id": movement_id},
        target_roles=["store_manager", "admin"],
    ))


# ---------------------------------------------------------------------------
# Day-End
# ---------------------------------------------------------------------------

async def dayend_reconciled(entity: str, *, scan_id: int,
                            floor_location: str) -> None:
    entity = _validate_entity(entity, "dayend.reconciled")
    await event_bus.publish(Event(
        event_type="dayend.reconciled",
        entity=entity,
        payload={"scan_id": scan_id, "floor_location": floor_location},
        target_roles=["planner", "admin"],
    ))


async def dayend_discrepancy_found(entity: str, *, discrepancy_type: str,
                                    severity: str,
                                    affected_material: str | None = None,
                                    affected_job_cards: int = 0) -> None:
    entity = _validate_entity(entity, "dayend.discrepancy_found")
    await event_bus.publish(Event(
        event_type="dayend.discrepancy_found",
        entity=entity,
        payload={"discrepancy_type": discrepancy_type, "severity": severity,
                 "affected_material": affected_material,
                 "affected_job_cards": affected_job_cards},
        target_roles=["planner", "store_manager", "admin"],
    ))


# ---------------------------------------------------------------------------
# Store Alerts
# ---------------------------------------------------------------------------

async def store_alert_created(entity: str, *, allocation_id: int,
                               decision: str, material: str,
                               approved_qty: float) -> None:
    entity = _validate_entity(entity, "store_alert.created")
    await event_bus.publish(Event(
        event_type="store_alert.created",
        entity=entity,
        payload={"allocation_id": allocation_id, "decision": decision,
                 "material": material, "approved_qty": approved_qty},
        target_roles=["store_manager", "admin"],
    ))
