"""FG kg comes from the boxes (decided with the user 2026-10-26/27).

On a box-producing stage (output_kind SFG/WIP) the batch's output is weighed
into SFG boxes on Boxes printing. Once a batch has boxes, their total IS its FG
kg — not a second figure to type and keep within ±0.5 kg of them:

* Open batch: FG kg = the live box total, whatever a client sends. /outputs,
  close_batch, PUT /accounting/summary and the accounting-record writer all
  replace a typed figure with it and say so (``replacement``), so an Android or
  API save is kept rather than refused.
* Completed (closed) batch: boxes made or re-weighed after the save can RAISE
  its FG, never lower it. The raise comes out of the batch's recorded process
  loss, so the batch stays balanced, and is allowed up to that loss + the 0.5 kg
  scale slack. The extra kg wait at the stage: the Dispatch button, completing
  or closing the card sends them on (send_on_remaining_output). Lighter boxes
  leave FG as it is (Boxes printing shows what is still to box) — nothing sent
  on is taken back.
* A batch whose FG can no longer move — this card is closed, or the next
  stage's card is already completed/closed — keeps today's cap: its FG + 0.5 kg.

Live boxes = sfg_box rows of the batch with item_type 'sfg' that are not
CANCELLED. Every check here runs BEFORE the first write of its caller: the box
routes raise only after their transaction has committed.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Stages whose output is boxed on Boxes printing.
BOXING_KINDS = ("SFG", "WIP")
# Two kg figures within half a gram are the same figure.
SAME_KG = 0.0005
_CANCELLED = "CANCELLED"


def _f(v) -> float | None:
    return None if v is None else float(v)


def _kg(v: float) -> str:
    return f"{v:.3f} kg"


def _boxes(n: int) -> str:
    return f"{n} box" if n == 1 else f"{n} boxes"


async def live_boxes(conn, batch_id: int) -> dict | None:
    """{kg, boxes} of the batch's live SFG boxes, or None when it has none."""
    row = await conn.fetchrow(
        "SELECT COUNT(*) AS boxes, COALESCE(SUM(net_weight), 0) AS kg FROM sfg_box "
        "WHERE batch_id = $1 AND item_type = 'sfg' AND status IS DISTINCT FROM $2",
        batch_id, _CANCELLED,
    )
    if not row or not row["boxes"]:
        return None
    return {"kg": round(float(row["kg"]), 3), "boxes": int(row["boxes"])}


async def fg_for_write(conn, job_card_id: int, batch_id: int | None) -> dict | None:
    """The FG kg a write to this batch must carry when its boxes decide it.

    Open batch → the live box total. Closed batch → its FG as it stands (raised
    by boxes, never typed). None when the typed figure stands: no batch, not a
    boxing stage, or no boxes yet.
    """
    if batch_id is None:
        return None
    row = await conn.fetchrow(
        "SELECT b.status, b.produced_qty_kg, b.fg_actual_kg, j.output_kind "
        "FROM job_card_batch_v2 b JOIN job_card_v2 j ON j.job_card_id = b.job_card_id "
        "WHERE b.batch_id = $1 AND b.job_card_id = $2",
        batch_id, job_card_id,
    )
    if row is None or (row["output_kind"] or "").upper() not in BOXING_KINDS:
        return None
    boxed = await live_boxes(conn, batch_id)
    if boxed is None:
        return None
    if row["status"] == "open":
        kg = boxed["kg"]
    else:
        fg = row["produced_qty_kg"] if row["produced_qty_kg"] is not None else row["fg_actual_kg"]
        kg = round(float(fg), 3) if fg is not None else boxed["kg"]
    return {"kg": kg, "boxes": boxed["boxes"], "box_kg": boxed["kg"], "status": row["status"]}


def replacement(typed, fg: dict) -> dict | None:
    """What to tell the client when the boxes replaced the FG kg it sent; None
    when it sent the same figure."""
    if typed is not None and abs(float(typed) - fg["kg"]) <= SAME_KG:
        return None
    sent = f" instead of the {_kg(float(typed))} sent" if typed is not None else ""
    return {
        "kg": fg["kg"],
        "typed_kg": _f(typed),
        "boxes": fg["boxes"],
        "message": f"FG kg comes from this batch's {_boxes(fg['boxes'])}: "
                   f"saved {_kg(fg['kg'])}{sent}.",
    }


async def log_replacement(conn, *, job_card_id: int, batch_id: int, note: dict,
                          actor: str | None) -> None:
    """Edit-log line for a typed FG the boxes replaced."""
    from app.modules.production.services.amendment_service import log_jc_field_changes
    await log_jc_field_changes(
        conn, job_card_id=job_card_id, record_type="job_card",
        field_prefix=f"fg_from_boxes:{batch_id}.", changed_by=actor,
        reason="FG kg comes from the boxes",
        before={"output_qty_kg": note["typed_kg"]}, after={"output_qty_kg": note["kg"]},
    )


async def take_fg(conn, *, job_card_id: int, batch_id: int | None, typed,
                  actor: str | None) -> tuple[float | None, dict | None]:
    """For a writer about to save FG kg ``typed`` on this batch: the figure to
    save and the note to return (None when nothing was replaced). A batch whose
    boxes don't decide its FG gets ``typed`` back unchanged."""
    fg = await fg_for_write(conn, job_card_id, batch_id)
    if fg is None:
        return typed, None
    note = replacement(typed, fg)
    if note is not None:
        await log_replacement(conn, job_card_id=job_card_id, batch_id=batch_id,
                              note=note, actor=actor)
        logger.info("FG from boxes: JC %s batch %s typed %s -> %s",
                    job_card_id, batch_id, typed, fg["kg"])
    return fg["kg"], note


async def stale_accounting(conn, *, job_card_id: int, batch_id: int,
                           box_kg: float) -> dict | None:
    """Refusal for completing a batch whose saved accounting balanced on another
    FG than its boxes (a box was added or re-weighed after the save). The batch
    has to be saved again first, so its balance is checked on the boxes."""
    saved = await conn.fetchval(
        "SELECT output_qty FROM job_card_accounting_v2 "
        "WHERE job_card_id = $1 AND COALESCE(batch_id, 0) = COALESCE($2::bigint, 0)",
        job_card_id, batch_id,
    )
    if saved is None or abs(float(saved) - box_kg) <= SAME_KG:
        return None
    return {
        "error": "accounting_stale",
        "saved_kg": float(saved),
        "box_kg": box_kg,
        "message": (f"This batch's boxes total {_kg(box_kg)}, but its accounting was saved "
                    f"with {_kg(float(saved))}. Save the batch again so it balances on the "
                    "boxes, then complete it."),
    }


# ── Box writes ────────────────────────────────────────────────────────────────

def _name(b) -> str:
    label = (b["batch_label"] or "").strip() if "batch_label" in b.keys() else ""
    return label or f"Batch {b['batch_number']}"


async def check_box_change(conn, *, job_card_id: int, totals_after: dict[int, float],
                           moved_out_of: set[int], tolerance_kg: float) -> tuple[dict | None, list[dict]]:
    """Judge a box create / edit before anything is written.

    ``totals_after``: each batch a box lands in → its live box total after the
    change. ``moved_out_of``: batches a box leaves. Returns (refusal, raises):
    a refusal dict, or the FG raises to apply once the boxes are written. The
    batch rows are locked FOR UPDATE here, in batch-id order, so the raise is
    worked out on the figures it will change.
    """
    ids = sorted(set(totals_after) | set(moved_out_of))
    if not ids:
        return None, []
    rows = await conn.fetch(
        "SELECT batch_id, batch_number, batch_label, status, produced_qty_kg, fg_actual_kg, "
        "       input_qty_kg, planned_qty_kg, process_loss_kg "
        "FROM job_card_batch_v2 WHERE job_card_id = $1 AND batch_id = ANY($2::bigint[]) "
        "ORDER BY batch_id FOR UPDATE",
        job_card_id, ids,
    )
    batches = {r["batch_id"]: r for r in rows}

    for bid in sorted(moved_out_of):
        b = batches.get(bid)
        if b is not None and b["status"] == "closed":
            return {
                "error": "box_in_completed_batch",
                "message": (f"{_name(b)} is completed, so its boxes can't move to another "
                            "batch — its FG kg was already counted with them."),
            }, []

    card = None
    raises: list[dict] = []
    for bid in sorted(totals_after):
        b = batches.get(bid)
        if b is None:
            continue                          # ownership is the caller's check
        total = round(float(totals_after[bid]), 3)
        if b["status"] == "cancelled":
            return {
                "error": "batch_cancelled",
                "message": f"{_name(b)} is cancelled — put these boxes under another batch.",
            }, []
        if b["status"] == "open":
            # Before the save the boxes ARE the batch's output; only what it
            # was opened with (input, else planned kg) bounds them.
            cap = next((float(v) for v in (b["input_qty_kg"], b["planned_qty_kg"])
                        if v is not None and float(v) > 0), None)
            if cap is not None and total > cap + tolerance_kg:
                return {
                    "error": "batch_over_capacity",
                    "message": f"Boxes for {_name(b)} would total {_kg(total)}, over the "
                               f"{_kg(cap)} it was opened with.",
                }, []
            continue

        # Completed batch: boxes may raise its FG, out of its process loss.
        fg_raw = b["produced_qty_kg"] if b["produced_qty_kg"] is not None else b["fg_actual_kg"]
        fg = round(float(fg_raw or 0), 3)
        if total <= fg + SAME_KG:
            continue                          # lighter or equal: FG stays
        if card is None:
            card = await conn.fetchrow(
                "SELECT j.status, n.job_card_number AS next_number, n.status AS next_status "
                "FROM job_card_v2 j LEFT JOIN job_card_v2 n "
                "  ON n.job_card_id = j.next_job_card_id AND n.deleted_at IS NULL "
                "WHERE j.job_card_id = $1",
                job_card_id,
            )
        frozen = None
        if card is not None and card["status"] in ("closed", "cancelled"):
            frozen = f"this job card is {card['status']}"
        elif card is not None and card["next_status"] in ("completed", "closed"):
            number = f" {card['next_number']}" if card["next_number"] else ""
            frozen = f"the next stage's job card{number} is already {card['next_status']}"
        if frozen:
            if total > fg + tolerance_kg:
                return {
                    "error": "batch_over_capacity",
                    "message": f"Boxes for {_name(b)} would total {_kg(total)}, over its FG "
                               f"{_kg(fg)}. Its FG can't go up any more: {frozen}.",
                }, []
            continue
        acct_loss = await conn.fetchval(
            "SELECT process_loss_qty FROM job_card_accounting_v2 "
            "WHERE job_card_id = $1 AND COALESCE(batch_id, 0) = $2",
            job_card_id, bid,
        )
        loss = max(0.0, float(acct_loss if acct_loss is not None else (b["process_loss_kg"] or 0)))
        if total > fg + loss + tolerance_kg:
            return {
                "error": "batch_over_capacity",
                "message": (f"Boxes for {_name(b)} would total {_kg(total)}: more than its FG "
                            f"{_kg(fg)} plus its recorded process loss {_kg(loss)}. Check the "
                            "box weights, or correct the batch's accounting."),
            }, []
        d = round(min(total - fg, loss), 3)
        if d > SAME_KG:
            raises.append({"batch_id": bid, "batch": _name(b), "from_kg": fg,
                           "to_kg": round(fg + d, 3), "raise_kg": d})
    return None, raises


async def apply_raises(conn, *, job_card_id: int, raises: list[dict],
                       actor: str | None) -> list[dict]:
    """Raise each completed batch's FG to its boxes, taking the same kg out of
    its process loss on the batch row, its output row and its accounting row —
    the balance difference is unchanged, the loss percentages are recomputed
    (as save_accounting computes them, on FG output). Logged and noted on the
    card. Nothing is dispatched here."""
    from app.modules.production.services.amendment_service import log_jc_field_changes
    from app.modules.production.services.balance_override import record_override_remark
    done = []
    for r in raises:
        d, bid = r["raise_kg"], r["batch_id"]
        await conn.execute(
            "UPDATE job_card_batch_v2 "
            "   SET produced_qty_kg = COALESCE(produced_qty_kg, 0) + $2, "
            "       fg_actual_kg    = COALESCE(fg_actual_kg, produced_qty_kg, 0) + $2, "
            "       process_loss_kg = GREATEST(COALESCE(process_loss_kg, 0) - $2, 0) "
            " WHERE batch_id = $1",
            bid, d,
        )
        await conn.execute(
            "UPDATE job_card_output_v2 "
            "   SET output_qty_kg   = output_qty_kg + $3, "
            "       process_loss_kg = GREATEST(COALESCE(process_loss_kg, 0) - $3, 0), "
            "       yield_pct       = CASE WHEN rm_consumed_kg > 0 "
            "                              THEN LEAST(round(((output_qty_kg + $3) / rm_consumed_kg * 100)::numeric, 3), 999.999) "
            "                              ELSE yield_pct END "
            " WHERE job_card_id = $1 AND batch_id = $2 AND deleted_at IS NULL",
            job_card_id, bid, d,
        )
        before = await conn.fetchrow(
            "SELECT output_qty, process_loss_qty FROM job_card_accounting_v2 "
            "WHERE job_card_id = $1 AND COALESCE(batch_id, 0) = $2",
            job_card_id, bid,
        )
        after = await conn.fetchrow(
            """
            UPDATE job_card_accounting_v2 a
               SET output_qty         = a.output_qty + $3,
                   process_loss_qty   = GREATEST(a.process_loss_qty - $3, 0),
                   process_loss_pct   = CASE WHEN a.output_qty + $3 > 0 THEN round(
                       ((GREATEST(a.process_loss_qty - $3, 0) + COALESCE(a.wastage_qty, 0))
                        / (a.output_qty + $3) * 100)::numeric, 3) END,
                   invisible_loss_pct = CASE WHEN a.output_qty + $3 > 0 THEN round(
                       round(((GREATEST(a.process_loss_qty - $3, 0) + COALESCE(a.wastage_qty, 0))
                              / (a.output_qty + $3) * 100)::numeric, 3)
                       + round((COALESCE(a.extra_give_away_qty, 0) / (a.output_qty + $3) * 100)::numeric, 3),
                       3) END,
                   total_loss_pct     = CASE WHEN a.output_qty + $3 > 0 THEN round(
                       ((GREATEST(a.process_loss_qty - $3, 0) + COALESCE(a.wastage_qty, 0)
                         + COALESCE(a.extra_give_away_qty, 0) + COALESCE(a.offgrade_total_qty, 0))
                        / (a.output_qty + $3) * 100)::numeric, 3) END
             WHERE a.job_card_id = $1 AND COALESCE(a.batch_id, 0) = $2
            RETURNING output_qty, process_loss_qty
            """,
            job_card_id, bid, d,
        )
        if before is not None and after is not None:
            await log_jc_field_changes(
                conn, job_card_id=job_card_id, record_type="job_card",
                field_prefix=f"accounting:{bid}.", changed_by=actor,
                reason="FG raised by the boxes",
                before={"output_qty": before["output_qty"], "process_loss_qty": before["process_loss_qty"]},
                after={"output_qty": after["output_qty"], "process_loss_qty": after["process_loss_qty"]},
            )
        await record_override_remark(
            conn, job_card_id=job_card_id, recorded_by=actor,
            content=(f"{r['batch']} FG raised from {_kg(r['from_kg'])} to {_kg(r['to_kg'])} by its "
                     f"boxes; its process loss is {_kg(d)} lower, so it still balances. The extra "
                     "goes on to the next stage with Dispatch, or when the card is completed or closed."),
        )
        logger.info("FG from boxes: JC %s batch %s raised %.3f -> %.3f kg",
                    job_card_id, bid, r["from_kg"], r["to_kg"])
        done.append({**r, "message": (f"{r['batch']}: FG raised to {_kg(r['to_kg'])} by its boxes "
                                      f"(process loss {_kg(d)} lower).")})
    return done
