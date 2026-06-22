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
_BOX_OPEN_STATUS = "PRINTED"
# A box can be received while it is still PRINTED or (once a dispatch step is
# wired) DISPATCHED — both are valid pre-receive states.
_BOX_RECEIVABLE_STATUSES = ("PRINTED", "DISPATCHED")
# JC states that have NOT produced anything yet (or are cancelled) → can't box.
_NOT_PRODUCING_STATUSES = (
    "locked", "unlocked", "assigned", "material_received", "cancelled",
)


async def create_wip_boxes(
    conn,
    job_card_id: int,
    boxes: list[dict],
    *,
    expected_net_kg: float | None = None,
    source_inventory_batch_id: str | None = None,
    parent_box_ids: list[int] | None = None,
) -> dict:
    """Mint ``sfg_box`` rows for a WIP-stage JC's physical box split.

    ``boxes``: list of ``{"net_weight": float, "gross_weight": float | None}``,
    one per physical box/bag. Each row gets an 8-digit app-supplied ``box_id``.

    Phase 7 genealogy wiring (FULLY active, no longer deferred):
      * ``lot_number`` is stamped on every new box from the source WIP
        ``inventory_batch.lot_number`` (looked up via ``source_inventory_batch_id``).
        If no source batch is given, or its lot is NULL, the box's lot_number is
        left NULL — never fabricated.
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
    # two operators) so the count-then-insert guard below can't be raced into a
    # double box set. Transaction-scoped advisory lock keyed on the JC id (an
    # 8-digit id never collides with helpers.SFG_CODE_LOCK). Backed by the
    # UNIQUE(job_card_id, box_number) constraint in 053 as a hard schema backstop.
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

    # Validate per-box weights.
    clean: list[tuple[float, float | None]] = []
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
        clean.append((nw, gw))
    if not clean:
        return {"error": "no_boxes", "message": "No boxes supplied"}

    total_net = round(sum(nw for nw, _ in clean), 3)
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

    # Don't double-split a JC (no phantom weight). Caller can GET existing first.
    already = await conn.fetchval(
        "SELECT count(*) FROM sfg_box WHERE job_card_id = $1", job_card_id
    )
    if already and already > 0:
        return {
            "error": "already_boxed",
            "message": f"Job card {job_card_id} already has {already} box(es)",
        }

    # Phase 7 (LOT STAMP): resolve the source WIP batch's lot so every box
    # carries it. Absent/NULL → leave NULL (don't fabricate).
    lot_number = None
    if source_inventory_batch_id:
        lot_number = await conn.fetchval(
            "SELECT lot_number FROM inventory_batch WHERE batch_id = $1",
            source_inventory_batch_id,
        )

    # Phase 7 (PARENT LINKAGE): normalise the optional box→box parents. Round-robin
    # by index so any N-new → M-parent re-box is covered (1:1 when counts match).
    parents: list[int] | None = None
    if parent_box_ids:
        parents = []
        for raw in parent_box_ids:
            try:
                parents.append(int(raw))
            except (TypeError, ValueError):
                return {"error": "bad_parent", "message": f"parent_box_id {raw!r} is not an int"}
        if not parents:
            parents = None

    total_boxes = len(clean)
    stage_bucket = jc["stage"] or "Create WIP"
    created: list[dict] = []
    for n, (nw, gw) in enumerate(clean, 1):
        parent_box_id = parents[(n - 1) % len(parents)] if parents else None
        # new_short_time_id() re-evaluated each retry so a same-ms PK collision
        # resolves with a fresh id; ON CONFLICT (box_id) is the PK so a true
        # collision raises *_pkey and insert_with_pk_retry retries.
        async def _insert(_n=n, _nw=nw, _gw=gw, _parent=parent_box_id):
            return await conn.fetchrow(
                """
                INSERT INTO sfg_box (
                    box_id, job_card_id, job_card_number, sfg_code, entity, floor,
                    stage_bucket, box_number, total_boxes, net_weight, gross_weight,
                    status, source_inventory_batch_id, lot_number, parent_box_id
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                RETURNING box_id, box_number, total_boxes, net_weight, gross_weight,
                          sfg_code, job_card_id, job_card_number, entity, floor,
                          stage_bucket, status, lot_number, parent_box_id,
                          source_inventory_batch_id
                """,
                new_short_time_id(), job_card_id, jc["job_card_number"], sfg_code,
                jc["entity"], jc["floor"], stage_bucket, _n, total_boxes, _nw, _gw,
                _BOX_OPEN_STATUS, source_inventory_batch_id, lot_number, _parent,
            )

        row = await insert_with_pk_retry(conn, _insert, max_retries=5)
        created.append(dict(row))

    logger.info(
        "SFG boxes: JC %s split into %d box(es), Σ=%.3f kg (%s)",
        job_card_id, total_boxes, total_net, sfg_code,
    )
    return {
        "job_card_id": job_card_id,
        "sfg_code": sfg_code,
        "total_net_kg": total_net,
        "box_ids": [r["box_id"] for r in created],
        "created": created,
    }


async def scan_receive_sfg_box(
    conn, downstream_job_card_id: int, box_ids: list[int], *, scanned_by: str | None = None
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
    seen: set[int] = set()

    for raw in box_ids or []:
        try:
            bid = int(raw)
        except (TypeError, ValueError):
            rejected.append({"box_id": raw, "reason": "Not a valid box id"})
            continue
        if bid in seen:  # same id twice in one scan batch
            rejected.append({"box_id": bid, "reason": "Duplicate box id in this scan"})
            continue
        seen.add(bid)

        box = await conn.fetchrow("SELECT * FROM sfg_box WHERE box_id = $1", bid)
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
             WHERE box_id = $1 AND status IN ('PRINTED', 'DISPATCHED')
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
            "_lot_number": box["lot_number"],
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
        SELECT box_id, job_card_id, job_card_number, sfg_code, entity, floor,
               stage_bucket, box_number, total_boxes, net_weight, gross_weight,
               status, source_inventory_batch_id, received_into_job_card_id,
               lot_number, parent_box_id, created_at
          FROM sfg_box
         WHERE job_card_id = $1
         ORDER BY box_number
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


async def get_box(conn, box_id: int) -> dict | None:
    """Single box lookup (mirror of GET /boxes/{box_id} for po_box)."""
    row = await conn.fetchrow("SELECT * FROM sfg_box WHERE box_id = $1", box_id)
    return dict(row) if row else None


# ══════════════════════════════════════════════════════════════════════════
#  PHASE 7 — GENEALOGY READ (box→box→lot)
# ══════════════════════════════════════════════════════════════════════════

# Recursion cap for the upstream box-chain walk (safety against any cyclic /
# self-referential parent_box_id data).
_GENEALOGY_MAX_DEPTH = 25

# The canonical box projection used by both genealogy endpoints.
_BOX_GENEALOGY_COLS = (
    "box_id, sfg_code, lot_number, parent_box_id, net_weight, status, "
    "source_inventory_batch_id, job_card_id, received_into_job_card_id, "
    "job_card_number, floor, entity, box_number, total_boxes"
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
        f"WHERE job_card_id = $1 ORDER BY box_number",
        job_card_id,
    )
    consumed_rows = await conn.fetch(
        f"SELECT {_BOX_GENEALOGY_COLS} FROM sfg_box "
        f"WHERE received_into_job_card_id = $1 ORDER BY job_card_id, box_number",
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


async def get_box_genealogy(conn, box_id: int,
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
        f"SELECT {_BOX_GENEALOGY_COLS} FROM sfg_box WHERE box_id = $1", box_id
    )
    if not start:
        return None
    if not _in_scope(start):
        # Start box is outside the caller's scope — treat as not found (the
        # endpoint also pre-checks this; defensive when called directly).
        return None

    chain: list[dict] = []
    visited: set[int] = set()
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
                f"SELECT {_BOX_GENEALOGY_COLS} FROM sfg_box WHERE box_id = $1",
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
                "ORDER BY job_card_id, box_number LIMIT 1",
                src_batch,
            )
            # Fall back to THIS box's own producing JC (boxes from the same batch
            # share it) when no other box references the batch.
            if producer_jc is None:
                producer_jc = row["job_card_id"]
            if producer_jc is not None:
                upstream_rows = await conn.fetch(
                    f"SELECT {_BOX_GENEALOGY_COLS} FROM sfg_box "
                    f"WHERE received_into_job_card_id = $1 ORDER BY job_card_id, box_number",
                    producer_jc,
                )
                for urow in upstream_rows:
                    if urow["box_id"] not in visited and _in_scope(urow):
                        frontier.append((urow, next_level))

    return {"box_id": box_id, "chain": chain, "truncated": truncated}
