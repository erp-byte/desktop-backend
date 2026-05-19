"""POST /api/v1/po/commit implementation.

Per-PO transaction. mode ∈ {create_only, update_only, upsert}:
    • create_only — INSERT new POs; existing (entity, po_number) → skipped_duplicates
    • update_only — UPDATE existing POs; missing → skipped_missing
    • upsert      — INSERT or UPDATE per row

Per-PO failures DO NOT abort the batch — they collect into `errors[]` and
the response is still 200. The 400/500 envelope is reserved for batch-level
problems (empty pos[], invalid entity, etc.).

Lines are replaced wholesale on update (DELETE + INSERT) — simpler and
matches the user's mental model that "the preview Excel is the source of
truth for this PO".
"""

from __future__ import annotations

import logging
from datetime import date as date_type, datetime, timezone
from typing import Any

from modules.purchase.services import po_event_log

logger = logging.getLogger(__name__)


_HEADER_COLUMNS: tuple[str, ...] = (
    "entity",
    "po_date",
    "voucher_type",
    "po_number",
    "order_reference_no",
    "narration",
    "vendor_supplier_name",
    "supplier_id",
    "gross_total",
    "total_amount",
    "sgst_amount",
    "cgst_amount",
    "igst_amount",
    "round_off",
    "freight_transport_local",
    "apmc_tax",
    "packing_charges",
    "freight_transport_charges",
    "loading_unloading_charges",
    "other_charges_non_gst",
)

_LINE_COLUMNS: tuple[str, ...] = (
    "sku_name",
    "uom",
    "pack_count",
    "po_weight",
    "rate",
    "amount",
    "particulars",
    "item_category",
    "sub_category",
    "item_type",
    "sales_group",
    "gst_rate",
    "match_score",
    "match_source",
)


def _coerce_date(v: Any) -> date_type | None:
    if v is None or v == "":
        return None
    if isinstance(v, date_type):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        return date_type.fromisoformat(str(v)[:10])
    except (ValueError, TypeError):
        return None


def _txn_no(po_idx: int, base: datetime) -> str:
    """Generate a stable transaction_no for newly-inserted POs.
    Uses base time + 1-second offset per PO so batches don't collide."""
    return f"TR-{base.strftime('%Y%m%d%H%M%S')}-{po_idx + 1:03d}"


async def _insert_po(conn, *, transaction_no: str, entity: str,
                     header: dict, lines: list[dict]) -> None:
    """INSERT a fresh po_header + po_line rows. Caller wraps in transaction."""
    h = dict(header)
    h["entity"] = entity
    h["po_date"] = _coerce_date(h.get("po_date"))
    await conn.execute(
        """
        INSERT INTO po_header (
            transaction_no, entity, po_date, voucher_type, po_number,
            order_reference_no, narration, vendor_supplier_name, supplier_id,
            gross_total, total_amount, sgst_amount, cgst_amount, igst_amount,
            round_off, freight_transport_local, apmc_tax, packing_charges,
            freight_transport_charges, loading_unloading_charges,
            other_charges_non_gst
        )
        VALUES (
            $1, $2, $3, $4, $5,
            $6, $7, $8, $9,
            $10, $11, $12, $13, $14,
            $15, $16, $17, $18,
            $19, $20,
            $21
        )
        """,
        transaction_no, entity, h.get("po_date"), h.get("voucher_type"), h.get("po_number"),
        h.get("order_reference_no"), h.get("narration"), h.get("vendor_supplier_name"),
        h.get("supplier_id"),
        h.get("gross_total"), h.get("total_amount"), h.get("sgst_amount"),
        h.get("cgst_amount"), h.get("igst_amount"),
        h.get("round_off"), h.get("freight_transport_local"), h.get("apmc_tax"),
        h.get("packing_charges"),
        h.get("freight_transport_charges"), h.get("loading_unloading_charges"),
        h.get("other_charges_non_gst"),
    )
    await _insert_lines(conn, transaction_no, lines)


async def _update_po(conn, *, transaction_no: str, entity: str,
                     header: dict, lines: list[dict]) -> None:
    """UPDATE po_header + replace po_line rows. Caller wraps in transaction."""
    h = dict(header)
    h["po_date"] = _coerce_date(h.get("po_date"))
    await conn.execute(
        """
        UPDATE po_header SET
            po_date                       = COALESCE($2,  po_date),
            voucher_type                  = COALESCE($3,  voucher_type),
            po_number                     = COALESCE($4,  po_number),
            order_reference_no            = COALESCE($5,  order_reference_no),
            narration                     = COALESCE($6,  narration),
            vendor_supplier_name          = COALESCE($7,  vendor_supplier_name),
            supplier_id                   = COALESCE($8,  supplier_id),
            gross_total                   = COALESCE($9,  gross_total),
            total_amount                  = COALESCE($10, total_amount),
            sgst_amount                   = COALESCE($11, sgst_amount),
            cgst_amount                   = COALESCE($12, cgst_amount),
            igst_amount                   = COALESCE($13, igst_amount),
            round_off                     = COALESCE($14, round_off),
            freight_transport_local       = COALESCE($15, freight_transport_local),
            apmc_tax                      = COALESCE($16, apmc_tax),
            packing_charges               = COALESCE($17, packing_charges),
            freight_transport_charges     = COALESCE($18, freight_transport_charges),
            loading_unloading_charges     = COALESCE($19, loading_unloading_charges),
            other_charges_non_gst         = COALESCE($20, other_charges_non_gst)
         WHERE transaction_no = $1
           AND entity = $21
           AND deleted_at IS NULL
        """,
        transaction_no,
        h.get("po_date"), h.get("voucher_type"), h.get("po_number"),
        h.get("order_reference_no"), h.get("narration"), h.get("vendor_supplier_name"),
        h.get("supplier_id"),
        h.get("gross_total"), h.get("total_amount"), h.get("sgst_amount"),
        h.get("cgst_amount"), h.get("igst_amount"),
        h.get("round_off"), h.get("freight_transport_local"), h.get("apmc_tax"),
        h.get("packing_charges"),
        h.get("freight_transport_charges"), h.get("loading_unloading_charges"),
        h.get("other_charges_non_gst"),
        entity,
    )
    # Replace lines wholesale
    await conn.execute("DELETE FROM po_line WHERE transaction_no = $1", transaction_no)
    await _insert_lines(conn, transaction_no, lines)


async def _insert_lines(conn, transaction_no: str, lines: list[dict]) -> None:
    for idx, line in enumerate(lines, start=1):
        ln = line.get("line_number") or idx
        await conn.execute(
            """
            INSERT INTO po_line (
                transaction_no, line_number, sku_name, uom, pack_count,
                po_weight, rate, amount, particulars, item_category,
                sub_category, item_type, sales_group, gst_rate,
                match_score, match_source
            )
            VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9, $10,
                $11, $12, $13, $14,
                $15, $16
            )
            """,
            transaction_no, ln, line.get("sku_name"), line.get("uom"),
            line.get("pack_count"),
            line.get("po_weight"), line.get("rate"), line.get("amount"),
            line.get("particulars"), line.get("item_category"),
            line.get("sub_category"), line.get("item_type"),
            line.get("sales_group"), line.get("gst_rate"),
            line.get("match_score"), line.get("match_source"),
        )


# ── main entrypoint ──────────────────────────────────────────────────────


async def commit(
    pool,
    *,
    entity: str,
    mode: str,
    pos: list[dict],
    actor_user_id: str | None,
    actor_role: str | None,
) -> dict:
    """Apply the batch. Per-PO failures land in `errors[]`.

    HI-02: each PO body is checked against `entity` (the batch-level entity).
    A per-PO `header.entity` or `duplicate_key` whose entity prefix doesn't
    match the batch is rejected into `errors[]` — the body's `extra='allow'`
    pydantic config means we can't catch these at the validation layer.
    """
    base_time = datetime.now(timezone.utc)
    out = {
        "created": [], "updated": [], "skipped_duplicates": [],
        "skipped_missing": [], "errors": [],
    }

    entity_lc = entity.lower()

    for po_idx, po in enumerate(pos):
        header = po.get("header") or {}
        lines = po.get("lines") or []
        po_number = header.get("po_number")
        duplicate_key = po.get("duplicate_key") or (
            f"{entity}|{po_number}" if po_number else f"{entity}|<no-po-number>"
        )
        provided_txn = po.get("transaction_no")

        try:
            if not header.get("vendor_supplier_name"):
                raise ValueError("vendor_supplier_name is required")
            if not po_number:
                raise ValueError("header.po_number is required")

            # HI-02: per-PO entity must match the batch entity
            per_po_entity = header.get("entity")
            if per_po_entity and str(per_po_entity).lower() != entity_lc:
                raise ValueError(
                    f"header.entity={per_po_entity!r} does not match batch entity={entity!r}"
                )

            # HI-02: duplicate_key prefix must match the batch entity (if provided)
            dk = po.get("duplicate_key")
            if dk and "|" in dk:
                dk_entity = dk.split("|", 1)[0]
                if dk_entity.lower() != entity_lc:
                    raise ValueError(
                        f"duplicate_key entity prefix {dk_entity!r} does not match batch entity={entity!r}"
                    )

            async with pool.acquire() as conn:
                async with conn.transaction():
                    # Find existing by (entity, po_number)
                    existing = await conn.fetchrow(
                        """
                        SELECT transaction_no FROM po_header
                         WHERE entity = $1 AND po_number = $2 AND deleted_at IS NULL
                        """,
                        entity, po_number,
                    )

                    if existing:
                        if mode == "create_only":
                            out["skipped_duplicates"].append(existing["transaction_no"])
                            continue
                        txn_no = existing["transaction_no"]
                        await _update_po(
                            conn, transaction_no=txn_no, entity=entity,
                            header=header, lines=lines,
                        )
                        out["updated"].append(txn_no)
                        await po_event_log.write(
                            conn, transaction_no=txn_no, entity=entity,
                            event_type=po_event_log.EVENT_UPDATED,
                            actor_user_id=actor_user_id, actor_role=actor_role,
                            payload={"po_number": po_number, "line_count": len(lines)},
                        )
                    else:
                        if mode == "update_only":
                            out["skipped_missing"].append(provided_txn or duplicate_key)
                            continue
                        # Use the preview's txn_no if it looks fresh, otherwise mint a new one
                        if provided_txn and not provided_txn.startswith("TXN-PREVIEW-"):
                            txn_no = provided_txn
                        else:
                            txn_no = _txn_no(po_idx, base_time)
                        await _insert_po(
                            conn, transaction_no=txn_no, entity=entity,
                            header=header, lines=lines,
                        )
                        out["created"].append(txn_no)
                        await po_event_log.write(
                            conn, transaction_no=txn_no, entity=entity,
                            event_type=po_event_log.EVENT_CREATED,
                            actor_user_id=actor_user_id, actor_role=actor_role,
                            payload={"po_number": po_number, "line_count": len(lines)},
                        )
        except Exception as e:
            out["errors"].append({
                "po_number": po_number,
                "transaction_no": provided_txn,
                "duplicate_key": duplicate_key,
                "reason": str(e),
            })
            logger.warning(
                "po.commit.row_failed po_number=%s txn=%s err=%r",
                po_number, provided_txn, e,
            )

    return out
