"""SFG box service (Slice 6) — physical WIP box/bag split + QR scan-verify.

Production-side mirror of the po_box / qr_service flow, against ``sfg_box``. A
WIP-stage job card (output_kind SFG/WIP) splits its net SFG output into weighed
boxes; each box gets an 8-digit application-supplied ``box_id`` (the QR payload)
minted via ``new_short_time_id()`` + ``insert_with_pk_retry()`` — the same id
convention as the rest of the SFG / Job-Card work. Downstream stages scan those
QRs to receive the SFG, rejecting any box whose SFG or source job-card doesn't
match (mirror of ``qr_service.receive_material_via_qr``).

DEFERRED (Slice 5 integration): linking + debiting the WIP ``inventory_batch``.
``source_inventory_batch_id`` is an OPTIONAL parameter the Slice-5 close-batch
hook will pass; until then it is NULL and the scan debit is a guarded no-op (it
only runs when a box actually carries a linked batch). So this service is correct
standalone and forward-compatible.
"""

import logging

from app.core.helpers import insert_with_pk_retry, new_short_time_id

logger = logging.getLogger(__name__)

# Σ(box net weights) may deviate from the stage's expected net SFG by at most
# this (kg) — covers scale rounding without letting phantom/short weight through.
WEIGHT_TOLERANCE_KG = 0.5

_WIP_OUTPUT_KINDS = ("SFG", "WIP")
_SFG_INPUT_KINDS = ("SFG", "WIP")
# Boxes are minted PENDING (weighed but label not yet printed). The per-box print
# action (update_wip_boxes with mark_printed) flips them to PRINTED. Both states
# are pre-receive and editable; a box can only be scanned downstream once PRINTED.
_BOX_PENDING_STATUS = "PENDING"
# A box can be received while it is still PRINTED or (once a dispatch step is
# wired) DISPATCHED — both are valid pre-receive states.
_BOX_RECEIVABLE_STATUSES = ("PRINTED", "DISPATCHED")
# JC states that have NOT produced anything yet (or are cancelled) → can't box.
_NOT_PRODUCING_STATUSES = (
    "locked", "unlocked", "assigned", "material_received", "cancelled",
)


async def _batch_cap_kg(conn, batch_id: int) -> float | None:
    """The accounting weight (kg) a batch's boxes must not exceed: the batch's
    produced → input → planned quantity, first one that is set and > 0. Returns
    None when the batch has no accounting weight yet (→ no cap enforced)."""
    batch = await conn.fetchrow(
        "SELECT produced_qty_kg, input_qty_kg, planned_qty_kg "
        "FROM job_card_batch_v2 WHERE batch_id = $1",
        batch_id,
    )
    if not batch:
        return None
    for f in ("produced_qty_kg", "input_qty_kg", "planned_qty_kg"):
        v = batch[f]
        if v is not None and float(v) > 0:
            return float(v)
    return None


async def create_wip_boxes(
    conn,
    job_card_id: int,
    boxes: list[dict],
    *,
    expected_net_kg: float | None = None,
    source_inventory_batch_id: str | None = None,
    parent_box_ids: list[str] | None = None,
) -> dict:
    """Mint ``sfg_box`` rows for a WIP-stage JC's physical box split.

    ``boxes``: list of ``{"net_weight": float, "gross_weight": float | None,
    "batch_code"|"lot"|"batch": str | None, "units"|"count": int | None}``, one
    per physical box/bag. Each row gets a TEXT ``box_id`` of the form
    ``"<8-digit-time-base>-<per-JC counter>"`` (e.g. ``"48213307-1"``), matching
    the po_box / RM box format. The counter CONTINUES from the last box created
    for this job card, so a second create call appends ``-N+1, -N+2, …`` rather
    than resetting — a JC can be split across multiple calls.

    Phase 7 genealogy wiring (FULLY active, no longer deferred):
      * ``parent_box_id`` (box→box link): when ``parent_box_ids`` is given (a
        downstream WIP stage re-boxing the SFG it consumed), each new box is
        linked to a source box. The mapping is ROUND-ROBIN by index
        (new box n → parent_box_ids[(n-1) % len]) so any N→M re-box is covered:
        1:1 when counts match, and a sensible fan-in/fan-out otherwise. Default
        None = top-level producer boxes → ``parent_box_id`` stays NULL.

    Returns ``{"created": [...], "total_net_kg", "box_ids", "sfg_code"}`` or an
    ``{"error", "message"}`` dict (the caller maps that to HTTP 400). MUST run
    inside an outer transaction (``insert_with_pk_retry`` uses savepoints).
    """
    # Serialise concurrent splits of the SAME job card (double-click / retry /
    # two operators) so the per-JC counter read below stays monotonic and two
    # calls can't mint the same "<base>-<counter>". Transaction-scoped advisory
    # lock keyed on the JC id (an 8-digit id never collides with
    # helpers.SFG_CODE_LOCK). carton_id (TEXT PK) is the hard schema backstop.
    await conn.execute("SELECT pg_advisory_xact_lock($1)", job_card_id)

    jc = await conn.fetchrow(
        """
        SELECT job_card_id, job_card_number, fg_sku_name, output_kind, output_code,
               entity, floor, stage, status
          FROM job_card_v2
         WHERE job_card_id = $1 AND deleted_at IS NULL
        """,
        job_card_id,
    )
    if not jc:
        return {"error": "not_found", "message": f"Job card {job_card_id} not found"}
    if (jc["output_kind"] or "").upper() not in _WIP_OUTPUT_KINDS:
        return {
            "error": "not_a_wip_stage",
            "message": "Boxes are produced only by a Create-WIP / intermediate stage "
                       "(output_kind SFG/WIP), not a terminal FG stage.",
        }
    if (jc["status"] or "") in _NOT_PRODUCING_STATUSES:
        return {
            "error": "not_producing",
            "message": f"Job card status '{jc['status']}' cannot produce boxes "
                       "(not started yet, or cancelled).",
        }
    sfg_code = jc["output_code"]
    if not sfg_code:
        return {
            "error": "no_sfg_code",
            "message": "This stage has no output SFG#### yet (approve the plan / load "
                       "routing first).",
        }

    # Validate per-box weights + optional batch code / unit count / batch link.
    clean: list[tuple[float, float | None, str | None, int | None, int | None]] = []
    for i, b in enumerate(boxes or [], 1):
        try:
            nw = round(float(b.get("net_weight")), 3)
        except (TypeError, ValueError):
            return {"error": "bad_weight", "message": f"Box {i}: net_weight is not a number"}
        if nw <= 0:
            return {"error": "bad_weight", "message": f"Box {i}: net_weight must be > 0"}
        gw_raw = b.get("gross_weight")
        gw = None
        if gw_raw not in (None, ""):
            try:
                gw = round(float(gw_raw), 3)
            except (TypeError, ValueError):
                return {"error": "bad_weight", "message": f"Box {i}: gross_weight is not a number"}
            if gw < nw:
                return {"error": "bad_weight", "message": f"Box {i}: gross_weight < net_weight"}
        # "Batch" (FE) maps to sfg_box.batch_code (lot_number was dropped in 067).
        bc_raw = b.get("batch_code") or b.get("lot") or b.get("batch")
        batch_code = str(bc_raw).strip() or None if bc_raw not in (None, "") else None
        u_raw = b.get("units") if b.get("units") is not None else b.get("count")
        units = None
        if u_raw not in (None, ""):
            try:
                units = int(u_raw)
            except (TypeError, ValueError):
                return {"error": "bad_units", "message": f"Box {i}: count is not an integer"}
            if units < 0:
                return {"error": "bad_units", "message": f"Box {i}: count must be >= 0"}
        # MANDATORY link to a job-card accounting batch (job_card_batch_v2.batch_id,
        # an 8-digit BIGINT). Every box must belong to a batch so the box→batch
        # capacity/grouping invariants hold; batch_code carries that batch's display
        # name. Validated against this JC's batches below.
        bid_raw = b.get("batch_id")
        if bid_raw in (None, ""):
            return {"error": "batch_required",
                    "message": f"Box {i}: select a batch — every box must be linked to an accounting batch"}
        try:
            batch_id = int(bid_raw)
        except (TypeError, ValueError):
            return {"error": "bad_batch", "message": f"Box {i}: batch_id is not an integer"}
        clean.append((nw, gw, batch_code, units, batch_id))
    if not clean:
        return {"error": "no_boxes", "message": "No boxes supplied"}

    # Verify every linked batch_id actually belongs to THIS job card (the FE only
    # offers this JC's batches; this guards a hand-crafted / stale payload from
    # tying a box to another JC's — or a non-existent — batch).
    linked_ids = {row[4] for row in clean if row[4] is not None}
    if linked_ids:
        rows = await conn.fetch(
            "SELECT batch_id FROM job_card_batch_v2 "
            "WHERE job_card_id = $1 AND batch_id = ANY($2::bigint[])",
            job_card_id, list(linked_ids),
        )
        missing = linked_ids - {r["batch_id"] for r in rows}
        if missing:
            return {
                "error": "bad_batch",
                "message": f"Batch id(s) {sorted(missing)} are not batches of this job card",
            }

    # Cap: a batch's total box net weight must not exceed its accounting weight.
    # Sum the new boxes per batch, add the batch's existing box net, reject the
    # whole call if any batch would overflow (WEIGHT_TOLERANCE_KG covers rounding).
    add_by_batch: dict[int, float] = {}
    for row in clean:
        if row[4] is not None:
            add_by_batch[row[4]] = add_by_batch.get(row[4], 0.0) + row[0]
    for bid, add_net in add_by_batch.items():
        cap = await _batch_cap_kg(conn, bid)
        if cap is None:
            continue
        existing = await conn.fetchval(
            "SELECT COALESCE(SUM(net_weight), 0) FROM sfg_box "
            "WHERE batch_id = $1 AND item_type = 'sfg'", bid,
        ) or 0
        total = round(float(existing) + add_net, 3)
        if total > cap + WEIGHT_TOLERANCE_KG:
            return {
                "error": "batch_over_capacity",
                "message": f"Boxes for this batch would total {total} kg, over the "
                           f"batch's accounting {cap} kg.",
            }

    total_net = round(sum(row[0] for row in clean), 3)
    if expected_net_kg is not None:
        try:
            exp = round(float(expected_net_kg), 3)
        except (TypeError, ValueError):
            exp = None
        if exp is not None and abs(total_net - exp) > WEIGHT_TOLERANCE_KG:
            return {
                "error": "weight_mismatch",
                "message": f"Σ box weights {total_net} kg differs from expected net "
                           f"{exp} kg by more than {WEIGHT_TOLERANCE_KG} kg",
            }

    # Per-JC continuing counter: box ids are "<base>-<counter>" and the counter
    # continues from the last box created for this JC (a second create call
    # appends -N+1, -N+2 … rather than resetting). Bare-numeric legacy ids (no
    # '-suffix') are excluded, so the first new box on a legacy JC starts at 1.
    last_counter = await conn.fetchval(
        """
        SELECT COALESCE(MAX(CAST(split_part(carton_id, '-', 2) AS INTEGER)), 0)
          FROM sfg_box
         WHERE job_card_id = $1
           AND split_part(carton_id, '-', 2) ~ '^[0-9]+$'
        """,
        job_card_id,
    )

    # Phase 7 (PARENT LINKAGE): normalise the optional box→box parents. Round-robin
    # by index so any N-new → M-parent re-box is covered (1:1 when counts match).
    parents: list[str] | None = None
    if parent_box_ids:
        parents = []
        for raw in parent_box_ids:
            pid = str(raw).strip()
            if not pid:
                return {"error": "bad_parent", "message": f"parent_box_id {raw!r} is not a valid box id"}
            parents.append(pid)
        if not parents:
            parents = None

    stage_bucket = jc["stage"] or "Create WIP"
    created: list[dict] = []
    for n, (nw, gw, batch_code, units, batch_id) in enumerate(clean, 1):
        counter = last_counter + n
        parent_box_id = parents[(n - 1) % len(parents)] if parents else None
        # box_id = "<8-digit-time-base>-<per-JC counter>". new_short_time_id() is
        # re-rolled INSIDE the retry so a same-ms cross-JC PK collision on
        # "<base>-1" resolves with a fresh base; the per-JC counter stays fixed.
        async def _insert(_nw=nw, _gw=gw, _parent=parent_box_id, _counter=counter,
                          _bc=batch_code, _u=units, _bid=batch_id):
            box_id = f"{new_short_time_id()}-{_counter}"
            return await conn.fetchrow(
                """
                INSERT INTO sfg_box (
                    carton_id, item_type, job_card_id, job_card_number, sfg_code,
                    entity, floor, stage_bucket, net_weight, gross_weight,
                    batch_code, units, status, source_inventory_batch_id, parent_box_id,
                    fg_sku_name, batch_id
                ) VALUES ($1,'sfg',$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                RETURNING carton_id AS box_id, net_weight, gross_weight, batch_code,
                          units, sfg_code, fg_sku_name, batch_id, job_card_id,
                          job_card_number, entity, floor, stage_bucket, status,
                          parent_box_id, source_inventory_batch_id
                """,
                box_id, job_card_id, jc["job_card_number"], sfg_code,
                jc["entity"], jc["floor"], stage_bucket, _nw, _gw,
                _bc, _u, _BOX_PENDING_STATUS, source_inventory_batch_id, _parent,
                jc["fg_sku_name"], _bid,
            )

        row = await insert_with_pk_retry(conn, _insert, max_retries=5)
        created.append(dict(row))

    logger.info(
        "SFG boxes: JC %s split into %d box(es), Σ=%.3f kg (%s)",
        job_card_id, len(clean), total_net, sfg_code,
    )
    return {
        "job_card_id": job_card_id,
        "sfg_code": sfg_code,
        "total_net_kg": total_net,
        "box_ids": [r["box_id"] for r in created],
        "created": created,
    }


async def scan_receive_sfg_box(
    conn, downstream_job_card_id: int, box_ids: list[str], *, scanned_by: str | None = None
) -> dict:
    """Scan SFG box QR ids into a downstream (consuming) job card.

    Verifies each box: exists; SFG matches the downstream stage's expected input
    SFG (``input_code``); the producing JC is this stage's chain predecessor
    (``prev_job_card_id``); not already received/consumed. A correct box is
    marked RECEIVED (and its linked WIP batch debited, if any — Slice-5 guarded).
    Mirrors the accepted/rejected shape of ``qr_service.receive_material_via_qr``.

    Phase 7: after the scan, a single aggregated ``internal_issue_note``
    (purpose='wip_transfer') records the source-lot → destination-JC transfer.
    The note write is wrapped (H1 pattern) so it can never fail the scan.
    """
    djc = await conn.fetchrow(
        """
        SELECT job_card_id, job_card_number, input_kind, input_code, prev_job_card_id,
               entity, floor
          FROM job_card_v2
         WHERE job_card_id = $1 AND deleted_at IS NULL
        """,
        downstream_job_card_id,
    )
    if not djc:
        return {"error": "not_found", "message": f"Job card {downstream_job_card_id} not found"}
    if (djc["input_kind"] or "").upper() not in _SFG_INPUT_KINDS:
        return {
            "error": "not_a_consumer",
            "message": "This stage does not consume SFG/WIP — nothing to scan in.",
        }

    expected_sfg = djc["input_code"]
    prev_jc = djc["prev_job_card_id"]
    # Fail closed: if BOTH verification anchors are absent we cannot tell a real
    # box from a guessed 8-digit id — reject the whole request rather than
    # accept anything. (A real downstream SFG stage always has a chain
    # predecessor from plan approval; input_code is added by routing.)
    if expected_sfg is None and prev_jc is None:
        return {
            "error": "unverifiable",
            "message": "Stage has neither an expected input SFG nor a chain predecessor; "
                       "cannot verify box scans (approve the plan / load routing first).",
        }

    accepted: list[dict] = []
    rejected: list[dict] = []
    seen: set[str] = set()

    for raw in box_ids or []:
        bid = str(raw).strip()
        if not bid:
            rejected.append({"box_id": raw, "reason": "Not a valid box id"})
            continue
        if bid in seen:  # same id twice in one scan batch
            rejected.append({"box_id": bid, "reason": "Duplicate box id in this scan"})
            continue
        seen.add(bid)

        box = await conn.fetchrow("SELECT * FROM sfg_box WHERE carton_id = $1", bid)
        if not box:
            rejected.append({"box_id": bid, "reason": "Box not found in system"})
            continue
        if box["status"] not in _BOX_RECEIVABLE_STATUSES:
            rejected.append({"box_id": bid, "reason": f"Box not receivable (status {box['status']})"})
            continue
        if expected_sfg and box["sfg_code"] != expected_sfg:
            rejected.append({
                "box_id": bid,
                "reason": f"Wrong SFG: box carries {box['sfg_code']}, "
                          f"this stage expects {expected_sfg}",
            })
            continue
        if prev_jc is not None and box["job_card_id"] != prev_jc:
            rejected.append({
                "box_id": bid,
                "reason": "Wrong source: box was not produced by this stage's predecessor",
            })
            continue

        # Claim the box atomically — the `status = PRINTED` guard makes a
        # concurrent double-scan land as 0 rows (rejected below), never a
        # double-consume.
        status = await conn.execute(
            """
            UPDATE sfg_box
               SET status = 'RECEIVED', received_into_job_card_id = $2
             WHERE carton_id = $1 AND status IN ('PRINTED', 'DISPATCHED')
            """,
            bid, downstream_job_card_id,
        )
        if status.rsplit(" ", 1)[-1] == "0":
            rejected.append({"box_id": bid, "reason": "Box already received (concurrent scan)"})
            continue

        # Slice-5 (guarded): debit the linked WIP inventory_batch if present.
        if box["source_inventory_batch_id"]:
            await conn.execute(
                """
                UPDATE inventory_batch
                   SET current_qty_kg = GREATEST(0, current_qty_kg - $2),
                       status = CASE WHEN (current_qty_kg - $2) <= 0 THEN 'ISSUED' ELSE status END,
                       updated_at = NOW()
                 WHERE batch_id = $1
                """,
                box["source_inventory_batch_id"], float(box["net_weight"]),
            )

        accepted.append({
            "box_id": bid,
            "sfg_code": box["sfg_code"],
            "net_weight": float(box["net_weight"]),
            "source_job_card_id": box["job_card_id"],
            # Phase 7 breadcrumbs for the aggregated issue note (not in API shape).
            "_source_inventory_batch_id": box["source_inventory_batch_id"],
            "_lot_number": None,  # lot_number dropped from sfg_box (mig 067)
            "_source_floor": box["floor"],
        })

    total_received = round(sum(a["net_weight"] for a in accepted), 3)
    logger.info(
        "SFG box scan on JC %s: %d accepted, %d rejected, %.3f kg",
        downstream_job_card_id, len(accepted), len(rejected), total_received,
    )

    # Phase 7 (ISSUE NOTE): one aggregated internal_issue_note per scan call,
    # linking source lot → destination JC. H1 pattern: a note failure must NOT
    # fail the scan (the boxes are already durably RECEIVED above).
    if accepted:
        try:
            # SAVEPOINT so a failing note INSERT can't poison the outer txn (which
            # would silently roll back the durably-RECEIVED boxes). H1 pattern.
            async with conn.transaction():
                await _record_wip_transfer_note(
                    conn, accepted=accepted, downstream_jc=djc,
                    total_received=total_received, scanned_by=scanned_by,
                )
        except Exception:  # noqa: BLE001 — never fail the scan on a note write
            logger.exception(
                "SFG box scan JC %s: internal_issue_note write failed (scan kept)",
                downstream_job_card_id,
            )
    # Strip the internal breadcrumb keys (prefixed `_`) from the API payload.
    accepted_public = [
        {k: v for k, v in a.items() if not k.startswith("_")} for a in accepted
    ]
    return {
        "job_card_id": downstream_job_card_id,
        "expected_sfg": expected_sfg,
        "boxes_scanned": len(box_ids or []),
        "boxes_accepted": len(accepted),
        "boxes_rejected": len(rejected),
        "total_received_kg": total_received,
        "accepted_boxes": accepted_public,
        "rejected_boxes": rejected,
    }


async def _record_wip_transfer_note(conn, *, accepted, downstream_jc,
                                    total_received, scanned_by):
    """Write ONE aggregated ``internal_issue_note`` for a WIP box scan, linking
    the source WIP lot/batch → the consuming JC. Reuses the same note-number
    scheme as ``inventory_service.create_internal_issue``. Best-effort: the
    caller wraps this in try/except so it can never fail the scan.

    Aggregation: one note per scan call. ``sku_name`` = the SFG#### code (all
    accepted boxes share it — the scan rejects wrong-SFG). ``batch_id`` = the
    source WIP inventory_batch (first accepted box's, since all boxes from one
    upstream stage/lot share it); ``quantity_kg`` = Σ received net weight.
    """
    from datetime import date as _date

    first = accepted[0]
    sfg_code = first["sfg_code"]
    src_batch = first.get("_source_inventory_batch_id")
    src_lot = first.get("_lot_number")
    src_floor = first.get("_source_floor")
    dest_floor = downstream_jc["floor"] or "wip_store"
    entity = downstream_jc["entity"]
    requested_by = scanned_by or "system"

    seq = await conn.fetchval(
        "SELECT COUNT(*) + 1 FROM internal_issue_note WHERE created_at::date = CURRENT_DATE"
    )
    note_number = f"IIN-{_date.today().strftime('%Y%m%d')}-{seq:03d}"
    note_id = await conn.fetchval(
        """
        INSERT INTO internal_issue_note
            (note_number, sku_name, batch_id, quantity_kg,
             source_floor, destination_floor, purpose, requested_by, status, entity)
        VALUES ($1,$2,$3,$4,$5,$6,'wip_transfer',$7,'completed',$8)
        RETURNING note_id
        """,
        note_number, sfg_code, src_batch, total_received,
        src_floor, dest_floor, requested_by, entity,
    )
    logger.info(
        "SFG box scan: internal_issue_note %s (%s) — lot %s, %s kg → JC %s",
        note_number, note_id, src_lot, total_received, downstream_jc["job_card_id"],
    )
    return note_id


async def get_boxes_for_jc(conn, job_card_id: int) -> dict:
    """All boxes produced by a WIP-stage JC + their reconciliation total."""
    rows = await conn.fetch(
        """
        SELECT carton_id AS box_id, item_type, job_card_id, job_card_number, sfg_code,
               fg_sku_name, entity, floor, stage_bucket, batch_id, batch_code,
               net_weight, gross_weight, units,
               status, source_inventory_batch_id, received_into_job_card_id,
               parent_box_id, so_number, created_by, created_at
          FROM sfg_box
         WHERE job_card_id = $1 AND item_type = 'sfg'
         ORDER BY created_at, carton_id
        """,
        job_card_id,
    )
    boxes = [dict(r) for r in rows]
    return {
        "job_card_id": job_card_id,
        "count": len(boxes),
        "total_net_kg": round(sum(float(b["net_weight"]) for b in boxes), 3),
        "boxes": boxes,
    }


async def get_box(conn, box_id: str) -> dict | None:
    """Single box/carton lookup (mirror of GET /boxes/{box_id} for po_box)."""
    row = await conn.fetchrow(
        "SELECT *, carton_id AS box_id FROM sfg_box WHERE carton_id = $1", box_id
    )
    return dict(row) if row else None


async def update_wip_boxes(conn, job_card_id: int, updates: list[dict],
                           *, changed_by: str | None = None) -> dict:
    """Edit mutable fields (net/gross weight, batch link, unit count) of existing
    SFG boxes — the Material-In "Update saved boxes" analogue.

    ``updates``: list of ``{"box_id": str, "net_weight": float, "gross_weight":
    float | None, "batch_code": str | None, "batch_id": int | None, "units":
    int | None, "mark_printed": bool}``. PENDING or PRINTED boxes of THIS job card
    (item_type='sfg') are editable — a box already scanned into a downstream stage
    (RECEIVED/CONSUMED) is weight-locked and lands in ``skipped`` instead. When
    ``mark_printed`` is true (the per-box print action), the box also transitions
    PENDING → PRINTED as its data is saved. MUST run in an outer txn.

    Returns ``{"updated": [...], "skipped": [{"box_id","reason"}]}`` or an
    ``{"error","message"}`` dict (mapped to HTTP 400 by the caller).
    """
    if not updates:
        return {"error": "no_boxes", "message": "No boxes to update"}

    # Validate every row up front (same rules as create) so a bad payload fails
    # before any row is written. clean = (carton_id, nw, gw, batch_code, units, batch_id, mark_printed).
    clean: list[tuple[str, float, float | None, str | None, int | None, int | None, bool]] = []
    for i, u in enumerate(updates, 1):
        cid = str(u.get("box_id") or u.get("carton_id") or "").strip()
        if not cid:
            return {"error": "bad_box", "message": f"Update {i}: missing box_id"}
        try:
            nw = round(float(u.get("net_weight")), 3)
        except (TypeError, ValueError):
            return {"error": "bad_weight", "message": f"Box {cid}: net_weight is not a number"}
        if nw <= 0:
            return {"error": "bad_weight", "message": f"Box {cid}: net_weight must be > 0"}
        gw_raw = u.get("gross_weight")
        gw = None
        if gw_raw not in (None, ""):
            try:
                gw = round(float(gw_raw), 3)
            except (TypeError, ValueError):
                return {"error": "bad_weight", "message": f"Box {cid}: gross_weight is not a number"}
            if gw < nw:
                return {"error": "bad_weight", "message": f"Box {cid}: gross_weight < net_weight"}
        bc_raw = u.get("batch_code")
        batch_code = str(bc_raw).strip() or None if bc_raw not in (None, "") else None
        u_raw = u.get("units")
        units = None
        if u_raw not in (None, ""):
            try:
                units = int(u_raw)
            except (TypeError, ValueError):
                return {"error": "bad_units", "message": f"Box {cid}: count is not an integer"}
            if units < 0:
                return {"error": "bad_units", "message": f"Box {cid}: count must be >= 0"}
        # Batch is mandatory — an edit may not clear a box's batch link (mirror of
        # create_wip_boxes). Assigning a batch to a previously-unlinked box is fine
        # (that's a non-null target); only a null/blank target is rejected.
        bid_raw = u.get("batch_id")
        if bid_raw in (None, ""):
            return {"error": "batch_required",
                    "message": f"Box {cid}: select a batch — every box must be linked to an accounting batch"}
        try:
            batch_id = int(bid_raw)
        except (TypeError, ValueError):
            return {"error": "bad_batch", "message": f"Box {cid}: batch_id is not an integer"}
        mark_printed = bool(u.get("mark_printed"))
        clean.append((cid, nw, gw, batch_code, units, batch_id, mark_printed))

    # Every linked batch must belong to THIS job card (mirror of create_wip_boxes).
    linked_ids = {row[5] for row in clean if row[5] is not None}
    if linked_ids:
        rows = await conn.fetch(
            "SELECT batch_id FROM job_card_batch_v2 "
            "WHERE job_card_id = $1 AND batch_id = ANY($2::bigint[])",
            job_card_id, list(linked_ids),
        )
        missing = linked_ids - {r["batch_id"] for r in rows}
        if missing:
            return {
                "error": "bad_batch",
                "message": f"Batch id(s) {sorted(missing)} are not batches of this job card",
            }

    # Only boxes the UPDATE below will actually touch (right JC, SFG, still
    # PENDING/PRINTED) leave their current batch; a skipped box keeps its batch_id
    # and must stay counted in `base`. Lock that exact set FOR UPDATE so a
    # concurrent status flip can't desync this check from the UPDATE (one txn).
    payload_ids = [row[0] for row in clean]
    upd_rows = await conn.fetch(
        "SELECT carton_id FROM sfg_box "
        "WHERE job_card_id = $1 AND item_type = 'sfg' "
        "AND status IN ('PENDING', 'PRINTED') AND carton_id = ANY($2::text[]) "
        "FOR UPDATE",
        job_card_id, payload_ids,
    )
    updatable = {r["carton_id"] for r in upd_rows}

    # Cap: after these edits, each batch a box will land in must stay within its
    # accounting weight. base = net of that batch's boxes NOT being moved by this
    # update; add = Σ new net of updatable boxes targeting it. (Boxes moving OUT
    # only reduce a batch, so checking target batches bounds the maximum.)
    target_add: dict[int, float] = {}
    for row in clean:
        if row[5] is not None and row[0] in updatable:
            target_add[row[5]] = target_add.get(row[5], 0.0) + row[1]
    for bid, add_net in target_add.items():
        cap = await _batch_cap_kg(conn, bid)
        if cap is None:
            continue
        base = await conn.fetchval(
            "SELECT COALESCE(SUM(net_weight), 0) FROM sfg_box "
            "WHERE batch_id = $1 AND item_type = 'sfg' AND carton_id != ALL($2::text[])",
            bid, list(updatable),
        ) or 0
        total = round(float(base) + add_net, 3)
        if total > cap + WEIGHT_TOLERANCE_KG:
            return {
                "error": "batch_over_capacity",
                "message": f"Boxes for this batch would total {total} kg, over the "
                           f"batch's accounting {cap} kg.",
            }

    from app.modules.production.services.amendment_service import log_jc_field_changes

    updated: list[dict] = []
    skipped: list[dict] = []
    for cid, nw, gw, batch_code, units, batch_id, mark_printed in clean:
        # Snapshot pre-edit values so the edit log records the real before→after
        # per changed field (the box-data audit that drives the FE red markers).
        old = await conn.fetchrow(
            "SELECT net_weight, gross_weight, batch_code, units FROM sfg_box "
            "WHERE carton_id = $1 AND job_card_id = $2 AND item_type = 'sfg'",
            cid, job_card_id,
        )
        # The status IN (PENDING,PRINTED) + jc + item_type guard means a wrong-JC,
        # non-SFG, or already-received box updates 0 rows → reported as skipped,
        # never a silent cross-tenant write. mark_printed ($8) flips PENDING →
        # PRINTED as the box's data is saved (the per-box print action); a plain
        # edit leaves the status untouched.
        row = await conn.fetchrow(
            """
            UPDATE sfg_box
               SET net_weight = $3, gross_weight = $4, batch_code = $5,
                   units = $6, batch_id = $7,
                   status = CASE WHEN $8 THEN 'PRINTED' ELSE status END
             WHERE carton_id = $1 AND job_card_id = $2
               AND item_type = 'sfg' AND status IN ('PENDING', 'PRINTED')
            RETURNING carton_id AS box_id, net_weight, gross_weight, batch_code,
                      units, batch_id, sfg_code, fg_sku_name, status
            """,
            cid, job_card_id, nw, gw, batch_code, units, batch_id, mark_printed,
        )
        if row is None:
            skipped.append({
                "box_id": cid,
                "reason": "Not found, not this job card's SFG box, or already received (not editable)",
            })
        else:
            updated.append(dict(row))
            # Audit each field that actually changed (no-ops skipped by the logger).
            if old is not None and changed_by:
                await log_jc_field_changes(
                    conn, job_card_id=job_card_id, record_type="job_card_box",
                    field_prefix=f"box:{cid}.", changed_by=changed_by, reason="box edit",
                    before={"net_weight": old["net_weight"], "gross_weight": old["gross_weight"],
                            "batch_code": old["batch_code"], "units": old["units"]},
                    after={"net_weight": row["net_weight"], "gross_weight": row["gross_weight"],
                           "batch_code": row["batch_code"], "units": row["units"]},
                )

    logger.info(
        "SFG boxes: JC %s updated %d box(es), skipped %d",
        job_card_id, len(updated), len(skipped),
    )
    return {"job_card_id": job_card_id, "updated": updated, "skipped": skipped}


# ══════════════════════════════════════════════════════════════════════════
#  PHASE 7 — GENEALOGY READ (box→box→lot)
# ══════════════════════════════════════════════════════════════════════════

# Recursion cap for the upstream box-chain walk (safety against any cyclic /
# self-referential parent_box_id data).
_GENEALOGY_MAX_DEPTH = 25

# The canonical box projection used by both genealogy endpoints.
_BOX_GENEALOGY_COLS = (
    "carton_id AS box_id, sfg_code, parent_box_id, net_weight, status, "
    "source_inventory_batch_id, job_card_id, received_into_job_card_id, "
    "job_card_number, floor, entity, item_type, batch_id, batch_code"
)


def _box_dict(row) -> dict:
    """Normalise an sfg_box row to the genealogy box shape (floats coerced)."""
    b = dict(row)
    if b.get("net_weight") is not None:
        b["net_weight"] = float(b["net_weight"])
    return b


async def get_jc_genealogy(conn, job_card_id: int,
                           allowed_entities: list[str] | None = None) -> dict:
    """Per-JC genealogy: boxes this JC PRODUCED + boxes it CONSUMED.

    produced = sfg_box WHERE job_card_id = id (this JC minted them).
    consumed = sfg_box WHERE received_into_job_card_id = id (scanned in here).
    Each consumed box also carries ``source_job_card_id`` = the producing box's
    job_card_id (the upstream stage that minted it).

    ``allowed_entities``: when a non-empty list, boxes outside those entities are
    OMITTED (a CONSUMED box can be cross-entity — scanned in from another legal
    entity — and must not leak to a scoped caller). None / empty = no restriction
    (admin / wildcard scope), matching the endpoint's entity-scope semantics.
    """
    scope = set(allowed_entities) if allowed_entities else None
    produced_rows = await conn.fetch(
        f"SELECT {_BOX_GENEALOGY_COLS} FROM sfg_box "
        f"WHERE job_card_id = $1 ORDER BY created_at, carton_id",
        job_card_id,
    )
    consumed_rows = await conn.fetch(
        f"SELECT {_BOX_GENEALOGY_COLS} FROM sfg_box "
        f"WHERE received_into_job_card_id = $1 ORDER BY job_card_id, created_at, carton_id",
        job_card_id,
    )
    produced = [_box_dict(r) for r in produced_rows
                if scope is None or r["entity"] in scope]
    consumed = []
    for r in consumed_rows:
        if scope is not None and r["entity"] not in scope:
            continue
        b = _box_dict(r)
        # The producing JC of a consumed box IS its job_card_id (contract).
        b["source_job_card_id"] = b["job_card_id"]
        consumed.append(b)
    return {"job_card_id": job_card_id, "produced": produced, "consumed": consumed}


async def get_box_genealogy(conn, box_id: str,
                            allowed_entities: list[str] | None = None) -> dict | None:
    """Walk UPSTREAM from a box, building an ordered ancestry ``chain``.

    Each chain entry is a box dict plus a ``level`` (0 = this box, increasing
    upstream). The walk follows, at each box:
      * ``parent_box_id``  → the source box this one was re-boxed from (box→box);
      * ``source_inventory_batch_id`` → the producing ``job_card_id`` → that JC's
        boxes that were CONSUMED into THIS box's producing JC (lot/batch hop).
    Depth-capped at ``_GENEALOGY_MAX_DEPTH``; a box already visited is not
    re-expanded (cycle / diamond safety).

    ``allowed_entities``: when a non-empty list, ancestor boxes outside those
    entities are NOT expanded into and never appear in the chain (an upstream
    stage can belong to another legal entity — that lineage must not leak to a
    scoped caller). None / empty = no restriction (admin / wildcard).

    The returned dict carries ``truncated``: True when the walk hit the depth
    cap with ancestry still unexpanded, so a consumer can tell a partial chain
    from a complete one rather than reading it as the full lineage.
    """
    scope = set(allowed_entities) if allowed_entities else None

    def _in_scope(r) -> bool:
        return scope is None or r["entity"] in scope

    start = await conn.fetchrow(
        f"SELECT {_BOX_GENEALOGY_COLS} FROM sfg_box WHERE carton_id = $1", box_id
    )
    if not start:
        return None
    if not _in_scope(start):
        # Start box is outside the caller's scope — treat as not found (the
        # endpoint also pre-checks this; defensive when called directly).
        return None

    chain: list[dict] = []
    visited: set[str] = set()
    truncated = False
    # BFS over upstream ancestors; `frontier` holds (box_row, level).
    frontier: list[tuple[object, int]] = [(start, 0)]
    while frontier:
        row, level = frontier.pop(0)
        bid = row["box_id"]
        if bid in visited:
            continue
        visited.add(bid)
        # producer_job_card_id alias = the box's own job_card_id (the JC that
        # produced this box) — the FE box-trace reads this field name.
        chain.append({**_box_dict(row), "producer_job_card_id": row["job_card_id"],
                      "level": level})
        if level >= _GENEALOGY_MAX_DEPTH:
            # Hit the recursion cap; if this node still has unexpanded ancestry
            # the returned chain is partial — flag it rather than truncate silently.
            if row["parent_box_id"] is not None or row["source_inventory_batch_id"]:
                truncated = True
            continue

        next_level = level + 1
        # 1) direct box→box parent.
        parent_id = row["parent_box_id"]
        if parent_id is not None and parent_id not in visited:
            prow = await conn.fetchrow(
                f"SELECT {_BOX_GENEALOGY_COLS} FROM sfg_box WHERE carton_id = $1",
                parent_id,
            )
            if prow and _in_scope(prow):
                frontier.append((prow, next_level))

        # 2) lot/batch hop: the WIP batch this box came from was produced by a
        #    JC. inventory_batch carries no producer-JC column, so resolve it via
        #    sfg_box itself — every box minted from that batch carries the
        #    producing job_card_id. That producing JC consumed (scanned-in) the
        #    upstream stage's boxes — those are this box's batch-level ancestors.
        src_batch = row["source_inventory_batch_id"]
        if src_batch:
            # Deterministic producer resolution: a batch can be referenced by
            # boxes from more than one JC (re-box / split) — ORDER BY so the
            # chosen producer is stable across runs, not whatever LIMIT 1 yields.
            producer_jc = await conn.fetchval(
                "SELECT job_card_id FROM sfg_box "
                "WHERE source_inventory_batch_id = $1 "
                "ORDER BY job_card_id, created_at, carton_id LIMIT 1",
                src_batch,
            )
            # Fall back to THIS box's own producing JC (boxes from the same batch
            # share it) when no other box references the batch.
            if producer_jc is None:
                producer_jc = row["job_card_id"]
            if producer_jc is not None:
                upstream_rows = await conn.fetch(
                    f"SELECT {_BOX_GENEALOGY_COLS} FROM sfg_box "
                    f"WHERE received_into_job_card_id = $1 ORDER BY job_card_id, created_at, carton_id",
                    producer_jc,
                )
                for urow in upstream_rows:
                    if urow["box_id"] not in visited and _in_scope(urow):
                        frontier.append((urow, next_level))

    return {"box_id": box_id, "chain": chain, "truncated": truncated}
