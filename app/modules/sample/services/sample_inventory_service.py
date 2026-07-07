"""Sample / NPD inventory accounting backbone (NPD plan §1).

Three postings, mirroring the Job Card module's discipline but using the sample
movement types and a 531-style FG-sample receipt:

  Step A  post_goods_issue    — consume OWN recipe RM (FIFO decrement + 265).
                                CUSTOMER-supplied / off-master lines are recorded
                                as skipped — NO inventory posting (we never owned
                                the stock). This is the rule that distinguishes
                                Trials (§3).
  Step B  receive_fg_sample    — mint the finished trial sample into the dedicated
                                R&D location (create_batch + 531 receipt).
  Step C  reverse_goods_issue  — re-credit issued batches and post a 266 mirror
                                of a 265 (cancellation / reversal).

Legal-entity axis is 'cfpl' (the sample-module convention — the physical R&D
location lives on inventory_batch.warehouse_id, which is free-text, so no master
table is needed). Required-vs-issued variance is captured silently into
sample_consumption_variance for the future costing workstream (cost columns
nullable, never shown to shop-floor roles).
"""
from __future__ import annotations

from app.modules.production.services import inventory_service
from app.modules.production.services.material_document_service import (
    MVT_FG_RECEIPT,
    MVT_GI_SAMPLE,
    MVT_REVERSE_SAMPLE,
    REF_SAMPLE_REQ,
    create_material_document,
)

# Dedicated R&D / trial-sample stock location (Step B target). inventory_batch
# location columns are free-text, so this is a constant rather than a master row.
RND_WAREHOUSE = "R&D"
RND_FLOOR = "sample_rnd_store"


def _actor(user) -> str:
    return getattr(user, "full_name", None) or str(getattr(user, "user_id", "system"))


def _is_own(line: dict) -> bool:
    """OWN, non-off-master lines hit inventory; everything else is recorded only."""
    return line.get("ownership", "OWN") != "CUSTOMER" and not line.get("is_off_master", False)


async def post_goods_issue(conn, *, lines: list[dict], reference_id, reference_type=REF_SAMPLE_REQ,
                           entity: str = "cfpl", from_warehouse: str | None = None, user,
                           variance_source: tuple | None = None, notes: str | None = None) -> dict:
    """Step A. `lines`: dicts with sku_name, qty (issued), uom, optionally
    ownership / is_off_master / required_qty. OWN lines FIFO-decrement stock and
    join the 265 document; customer / off-master lines are returned under
    `skipped` with no posting. `variance_source=(source_type, source_id)` records
    the required-vs-issued variance silently. Returns {mat_doc_id, posted, skipped}."""
    actor = _actor(user)
    posted, skipped, doc_lines = [], [], []
    for ln in lines:
        sku = ln["sku_name"]
        qty = float(ln.get("qty") or 0)
        if not _is_own(ln):
            skipped.append({"sku_name": sku, "qty": qty,
                            "reason": "customer_supplied" if ln.get("ownership") == "CUSTOMER" else "off_master"})
            continue
        batch_id = None
        if qty > 0:
            batches = await inventory_service.get_available_batches(conn, sku, entity)
            if batches:
                try:
                    await inventory_service.issue_from_batch(
                        conn, batches[0]["batch_id"], qty, performed_by=actor, skip_fifo_check=True)
                    batch_id = batches[0]["batch_id"]
                except ValueError:
                    # Insufficient physical stock — still document the issue so the
                    # paper trail is complete; nothing to decrement.
                    batch_id = None
        doc_lines.append({
            "sku_name": sku, "batch_id": batch_id, "quantity_kg": qty, "uom": ln.get("uom", "kg"),
            "from_location": from_warehouse or "SAMPLE_RM_STORE", "to_location": "SAMPLE",
            "from_status": "AVAILABLE", "to_status": "ISSUED"})
        posted.append({"sku_name": sku, "qty": qty, "batch_id": batch_id})

    mat_doc = None
    if doc_lines:
        mat_doc = await create_material_document(
            conn, movement_type=MVT_GI_SAMPLE, reference_type=reference_type,
            reference_id=str(reference_id), created_by=actor, entity=entity,
            lines=doc_lines, notes=notes)

    if variance_source:
        src_type, src_id = variance_source
        for ln in lines:
            if _is_own(ln) and ln.get("required_qty") is not None:
                await _record_variance(
                    conn, source_type=src_type, source_id=src_id, sku_name=ln["sku_name"],
                    required=float(ln["required_qty"]), issued=float(ln.get("qty") or 0),
                    uom=ln.get("uom", "kg"))

    return {"mat_doc_id": mat_doc["mat_doc_id"] if mat_doc else None,
            "posted": posted, "skipped": skipped}


async def receive_fg_sample(conn, *, sku_name: str, qty_kg, reference_id,
                            reference_type=REF_SAMPLE_REQ, entity: str = "cfpl",
                            uom: str = "kg", lot_number: str | None = None, user) -> dict:
    """Step B — mint the finished trial sample into the R&D location and post the
    531 FG-sample receipt. Returns {batch_id, mat_doc_id}."""
    actor = _actor(user)
    qty = float(qty_kg)
    batch_id = f"FGSMP-{reference_type}-{reference_id}"
    await inventory_service.create_batch(
        conn, batch_id=batch_id, sku_name=sku_name, item_type="fg", source="PRODUCTION",
        qty_kg=qty, warehouse_id=RND_WAREHOUSE, floor_id=RND_FLOOR, entity=entity,
        lot_number=lot_number, performed_by=actor)
    mat_doc = await create_material_document(
        conn, movement_type=MVT_FG_RECEIPT, reference_type=reference_type,
        reference_id=str(reference_id), created_by=actor, entity=entity,
        lines=[{"sku_name": sku_name, "batch_id": batch_id, "quantity_kg": qty, "uom": uom,
                "from_location": "production_floor", "to_location": f"{RND_WAREHOUSE}:{RND_FLOOR}",
                "from_status": "production", "to_status": "AVAILABLE"}],
        notes=f"FG-sample receipt ({reference_type} {reference_id})")
    return {"batch_id": batch_id, "mat_doc_id": mat_doc["mat_doc_id"]}


async def issue_named_batch(conn, *, batch_id: str, sku_name: str, qty_kg,
                            reference_id, reference_type, entity: str = "cfpl",
                            uom: str = "kg", to_location: str = "ISSUED",
                            user, notes: str | None = None) -> dict:
    """Step C — issue a SPECIFIC batch out (e.g. the FG sample from R&D on
    dispatch). Decrements the named batch and posts a 265. Returns {mat_doc_id}."""
    actor = _actor(user)
    qty = float(qty_kg)
    try:
        await inventory_service.issue_from_batch(conn, batch_id, qty, performed_by=actor, skip_fifo_check=True)
    except ValueError:
        pass  # batch missing / insufficient — still document the issue below
    mat_doc = await create_material_document(
        conn, movement_type=MVT_GI_SAMPLE, reference_type=reference_type,
        reference_id=str(reference_id), created_by=actor, entity=entity,
        lines=[{"sku_name": sku_name, "batch_id": batch_id, "quantity_kg": qty, "uom": uom,
                "from_location": f"{RND_WAREHOUSE}:{RND_FLOOR}", "to_location": to_location,
                "from_status": "AVAILABLE", "to_status": "ISSUED"}],
        notes=notes)
    return {"mat_doc_id": mat_doc["mat_doc_id"]}


async def reverse_goods_issue(conn, *, mat_doc_id: str, entity: str = "cfpl",
                              user, reason: str | None = None) -> dict:
    """Step C reversal — re-credit each issued batch and post a 266 mirror of the
    original 265 (swapping from/to). Use on cancellation."""
    actor = _actor(user)
    orig = await conn.fetch(
        "SELECT * FROM material_document_line WHERE mat_doc_id = $1 ORDER BY line_number", mat_doc_id)
    rev_lines = []
    for ln in orig:
        qty = float(ln["quantity_kg"] or 0)
        if ln["batch_id"] and qty > 0:
            await conn.execute(
                """UPDATE inventory_batch
                      SET current_qty_kg = current_qty_kg + $2, status = 'AVAILABLE', updated_at = NOW()
                    WHERE batch_id = $1""", ln["batch_id"], qty)
            await conn.execute(
                """INSERT INTO inventory_event_log
                      (batch_id, event_type, to_status, quantity_kg, reference_type, performed_by, notes)
                   VALUES ($1, 'RETURNED', 'AVAILABLE', $2, 'sample_reversal', $3, $4)""",
                ln["batch_id"], qty, actor, reason)
        rev_lines.append({
            "sku_name": ln["sku_name"], "batch_id": ln["batch_id"], "quantity_kg": qty, "uom": ln["uom"],
            "from_location": ln["to_location"], "to_location": ln["from_location"],
            "from_status": ln["to_status"], "to_status": ln["from_status"], "lot_number": ln["lot_number"]})
    return await create_material_document(
        conn, movement_type=MVT_REVERSE_SAMPLE, reference_type="SAMPLE_REVERSAL",
        reference_id=mat_doc_id, created_by=actor, entity=entity, lines=rev_lines,
        reversal_of=mat_doc_id, notes=reason)


async def _record_variance(conn, *, source_type, source_id, sku_name, required, issued, uom) -> None:
    await conn.execute(
        """INSERT INTO sample_consumption_variance
              (source_type, source_id, material_sku_name, required_qty, issued_qty, uom)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        source_type, source_id, sku_name, required, issued, uom)
