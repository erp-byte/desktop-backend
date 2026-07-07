"""Reconcile an incoming SO file against an existing so_header.

The upload pipelines (sales register + SO book) used to skip duplicates
outright. They now call into this module to reconcile each repeat SO
against its DB state:

- Header metadata is refreshed in-place (customer name, voucher type, etc.).
- For every incoming row we match the article against the existing so_line
  rows by sku_name. If the article isn't on file under this SO, we append
  it as a new line (next line_number, fresh GST recon).
- If the article IS on file, we compare the incoming quantity against
  the existing total.  We can't reduce an order line whose qty has
  already been committed to production, so:
    incoming > existing  →  bump so_line + so_fulfillment_v2 by the delta
    incoming = existing  →  refresh metadata only
    incoming < existing  →  emit a warning, leave the row alone

so_line.quantity is the Excel "Qty." (pack count) and so_line.quantity_units
is the computed kg total (quantity × master.uom). The fulfillment_v2 sync
maps quantity_units → original_qty_kg and quantity → original_qty_units,
so deltas need to flow both ways too.

Per-line outcomes are surfaced in the response so the operator can
chase down any "warning" rows manually (the typical fix is a
revise_order on the fulfillment row).
"""

import logging
from datetime import date

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.modules.so.services.gst_reconciliation import reconcile_line

logger = logging.getLogger(__name__)


# Numeric jitter tolerance — Excel often round-trips through float, so we
# treat any delta within ±0.001 kg / packs as "unchanged" rather than
# generating spurious warnings.
_EPS = 0.001


def _norm_sku(name) -> str:
    if name is None:
        return ""
    return str(name).strip().lower()


def _to_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


async def reconcile_existing_so(
    conn,
    *,
    so_id: int,
    so_number: str,
    so_date: date | None,
    header_meta: dict,
    normalized_rows: list[dict],
) -> dict:
    """Reconcile an incoming SO payload against an existing so_header.

    Parameters
    ----------
    conn : asyncpg.Connection
        Already inside a transaction supplied by the caller.
    so_id, so_number : int, str
        Identify the existing SO.
    so_date : date | None
        Parsed SO date from the incoming file (used to refresh the
        header — fulfillment_v2 FY is keyed off the original date and
        is not re-derived here).
    header_meta : dict
        Optional keys: customer_name, common_customer_name, company,
        voucher_type. Anything provided overwrites the existing header.
    normalized_rows : list[dict]
        Each row is a dict with the keys consumed below. Both upload
        formats build this shape before calling in so the reconciler
        stays format-agnostic.

    Returns
    -------
    dict with:
        lines: list of {line, gst_recon, reconcile_status, ...} (same
               shape as the fresh-ingest path, with reconcile_* added)
        gst_ok, gst_mismatch, gst_warning : int (delta this call)
        matched_lines, unmatched_lines : int (delta this call)
        added_line_count, qty_bumped_count, qty_warning_count : int
    """

    # 1. Refresh header in-place. None values in header_meta blank the
    #    field — callers should drop the key if they want to preserve.
    await conn.execute(
        """
        UPDATE so_header SET
            so_date = COALESCE($2, so_date),
            customer_name = COALESCE($3, customer_name),
            common_customer_name = COALESCE($4, common_customer_name),
            company = COALESCE($5, company),
            voucher_type = COALESCE($6, voucher_type),
            extraction_status = 'extracted'
        WHERE so_id = $1
        """,
        so_id,
        so_date,
        header_meta.get("customer_name"),
        header_meta.get("common_customer_name"),
        header_meta.get("company"),
        header_meta.get("voucher_type"),
    )

    # 2. Load existing lines for SKU lookup. We key by trimmed-lower
    #    sku_name. If two incoming rows share a SKU, the first one
    #    consumes the existing row and the second falls through to
    #    "new line" — that mirrors how production treats them as
    #    separate consumable lots.
    existing_rows = await conn.fetch(
        """
        SELECT so_line_id, line_number, sku_name, quantity, quantity_units,
               rate_inr, amount_inr, total_amount_inr
        FROM so_line
        WHERE so_id = $1
        ORDER BY line_number
        """,
        so_id,
    )
    existing_by_sku: dict[str, dict] = {}
    max_line_number = 0
    for r in existing_rows:
        rd = dict(r)
        max_line_number = max(max_line_number, rd["line_number"] or 0)
        key = _norm_sku(rd["sku_name"])
        if key and key not in existing_by_sku:
            existing_by_sku[key] = rd

    # Track which existing lines were consumed by an incoming row so
    # duplicate-SKU incoming rows route correctly.
    consumed_keys: set[str] = set()

    results: list[dict] = []
    matched_delta = 0
    unmatched_delta = 0
    gst_ok = gst_mismatch = gst_warning = 0
    added = bumped = warned = 0

    for row in normalized_rows:
        sku_name = row.get("sku_name")
        key = _norm_sku(sku_name)
        matched_item = row.get("matched_item")
        score = row.get("score") or 0.0
        recon_input = row.get("recon_input") or {}
        rate_type = row.get("rate_type")
        computed_qty_units = row.get("computed_qty_units")

        if matched_item:
            matched_delta += 1
        else:
            unmatched_delta += 1

        # Helper to run GST recon + record per-line counts and bundle the
        # output into the SOLineWithRecon shape the upload responds with.
        async def _attach_gst_and_pack(*, so_line_id: int, line_number: int,
                                       reconcile_status: str,
                                       reconcile_note: str | None,
                                       qty_delta_kg: float | None,
                                       qty_delta_units: float | None,
                                       refresh_recon: bool):
            nonlocal gst_ok, gst_mismatch, gst_warning
            recon = reconcile_line(recon_input, matched_item)
            recon["match_score"] = score if matched_item else None

            if recon["status"] == "ok":
                gst_ok += 1
            elif recon["status"] == "mismatch":
                gst_mismatch += 1
            else:
                gst_warning += 1

            if refresh_recon:
                # Replace the previous recon row so the response and the
                # /gst-recon endpoint reflect the new quantities.
                await conn.execute(
                    "DELETE FROM so_gst_reconciliation WHERE so_line_id = $1",
                    so_line_id,
                )
                await conn.execute(
                    """
                    INSERT INTO so_gst_reconciliation (
                        so_line_id, so_id, expected_gst_rate, actual_gst_rate,
                        expected_gst_amount, actual_gst_amount, gst_difference,
                        gst_type, gst_type_valid, sgst_cgst_equal,
                        total_with_gst_valid, uom_match, item_type_flag,
                        rate_type, status, notes,
                        matched_item_description, matched_item_type,
                        matched_item_category, matched_sub_category,
                        matched_sales_group, matched_uom, match_score
                    )
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                            $17,$18,$19,$20,$21,$22,$23)
                    """,
                    so_line_id, so_id,
                    recon["expected_gst_rate"], recon["actual_gst_rate"],
                    recon["expected_gst_amount"], recon["actual_gst_amount"],
                    recon["gst_difference"],
                    recon["gst_type"], recon["gst_type_valid"],
                    recon["sgst_cgst_equal"], recon["total_with_gst_valid"],
                    recon["uom_match"], recon["item_type_flag"],
                    recon["rate_type"], recon["status"], recon["notes"],
                    recon["matched_item_description"], recon["matched_item_type"],
                    recon["matched_item_category"], recon["matched_sub_category"],
                    recon["matched_sales_group"], recon["matched_uom"],
                    recon["match_score"],
                )

            results.append({
                "line": {
                    "so_line_id": so_line_id,
                    "line_number": line_number,
                    "sku_name": sku_name,
                    "item_category": row.get("item_category"),
                    "sub_category": row.get("sub_category"),
                    "uom": row.get("uom"),
                    "grp_code": row.get("grp_code"),
                    "quantity": row.get("quantity"),
                    "quantity_units": computed_qty_units,
                    "rate_inr": row.get("rate_inr"),
                    "rate_type": rate_type,
                    "amount_inr": row.get("amount_inr"),
                    "igst_amount": row.get("igst_amount"),
                    "sgst_amount": row.get("sgst_amount"),
                    "cgst_amount": row.get("cgst_amount"),
                    "total_amount_inr": row.get("total_amount_inr"),
                    "apmc_amount": row.get("apmc_amount"),
                    "packing_amount": row.get("packing_amount"),
                    "freight_amount": row.get("freight_amount"),
                    "processing_amount": row.get("processing_amount"),
                    "item_type": matched_item.item_type if matched_item else None,
                    "item_description": matched_item.particulars if matched_item else None,
                    "sales_group": matched_item.sale_group if matched_item else None,
                    "match_score": score if matched_item else None,
                    "match_source": "all_sku" if matched_item else None,
                    "status": "pending",
                },
                "gst_recon": {
                    "so_line_id": so_line_id,
                    "line_number": line_number,
                    "sku_name": sku_name,
                    **{k: recon[k] for k in recon},
                },
                "reconcile_status": reconcile_status,
                "reconcile_note": reconcile_note,
                "qty_delta_kg": qty_delta_kg,
                "qty_delta_units": qty_delta_units,
            })

        existing = existing_by_sku.get(key) if key else None
        if existing is not None and key not in consumed_keys:
            consumed_keys.add(key)
            so_line_id = existing["so_line_id"]
            existing_qty = _to_float(existing.get("quantity")) or 0.0
            existing_kg = _to_float(existing.get("quantity_units")) or 0.0
            incoming_qty = _to_float(row.get("quantity")) or 0.0
            incoming_kg = _to_float(computed_qty_units) or 0.0

            delta_qty = incoming_qty - existing_qty
            delta_kg = incoming_kg - existing_kg

            # The kg delta is authoritative for production accounting,
            # but Excel files often carry pack count without a recomputed
            # kg total — fall back to the qty delta if computed_qty_units
            # is null on both sides.
            if computed_qty_units is None and existing.get("quantity_units") is None:
                # No kg info either side; let pack delta drive the decision.
                effective_delta = delta_qty
            else:
                effective_delta = delta_kg

            if effective_delta > _EPS:
                # Positive — extend the order. Add the delta to so_line
                # AND to so_fulfillment_v2 so the planning surface sees
                # the new ordered total. Other fields (rate, amounts) are
                # refreshed to the incoming values since an extension
                # often comes with a re-quote.
                new_quantity = existing_qty + delta_qty if delta_qty > 0 else existing_qty
                new_quantity_units = existing_kg + delta_kg if delta_kg > 0 else existing_kg
                await conn.execute(
                    """
                    UPDATE so_line SET
                        quantity = $2,
                        quantity_units = $3,
                        item_category = COALESCE($4, item_category),
                        sub_category = COALESCE($5, sub_category),
                        uom = COALESCE($6, uom),
                        grp_code = COALESCE($7, grp_code),
                        rate_inr = COALESCE($8, rate_inr),
                        rate_type = COALESCE($9, rate_type),
                        amount_inr = COALESCE($10, amount_inr),
                        igst_amount = COALESCE($11, igst_amount),
                        sgst_amount = COALESCE($12, sgst_amount),
                        cgst_amount = COALESCE($13, cgst_amount),
                        apmc_amount = COALESCE($14, apmc_amount),
                        packing_amount = COALESCE($15, packing_amount),
                        freight_amount = COALESCE($16, freight_amount),
                        processing_amount = COALESCE($17, processing_amount),
                        total_amount_inr = COALESCE($18, total_amount_inr),
                        item_type = COALESCE($19, item_type),
                        item_description = COALESCE($20, item_description),
                        sales_group = COALESCE($21, sales_group),
                        match_score = COALESCE($22, match_score),
                        match_source = COALESCE($23, match_source)
                    WHERE so_line_id = $1
                    """,
                    so_line_id,
                    new_quantity,
                    int(round(new_quantity_units)) if new_quantity_units is not None else None,
                    row.get("item_category"),
                    row.get("sub_category"),
                    row.get("uom"),
                    row.get("grp_code"),
                    row.get("rate_inr"),
                    rate_type,
                    row.get("amount_inr"),
                    row.get("igst_amount"),
                    row.get("sgst_amount"),
                    row.get("cgst_amount"),
                    row.get("apmc_amount"),
                    row.get("packing_amount"),
                    row.get("freight_amount"),
                    row.get("processing_amount"),
                    row.get("total_amount_inr"),
                    matched_item.item_type if matched_item else None,
                    matched_item.particulars if matched_item else None,
                    matched_item.sale_group if matched_item else None,
                    score if matched_item else None,
                    "all_sku" if matched_item else None,
                )

                # Bump the live fulfillment row. pending_qty_kg /
                # pending_qty_units are GENERATED — they recompute on
                # their own. We add the delta rather than overwrite the
                # absolute so produced/dispatched ratios survive.
                if delta_kg > 0 or delta_qty > 0:
                    await conn.execute(
                        """
                        UPDATE so_fulfillment_v2 SET
                            original_qty_kg = original_qty_kg + $2,
                            original_qty_units = COALESCE(original_qty_units, 0) + $3,
                            updated_at = NOW()
                        WHERE so_line_id = $1
                        """,
                        so_line_id,
                        max(delta_kg, 0.0),
                        max(delta_qty, 0.0),
                    )

                bumped += 1
                await _attach_gst_and_pack(
                    so_line_id=so_line_id,
                    line_number=existing["line_number"],
                    reconcile_status="bumped",
                    reconcile_note=(
                        f"Extended by +{delta_kg:.3f} kg / +{delta_qty:.3f} units"
                    ),
                    qty_delta_kg=delta_kg if delta_kg > 0 else 0.0,
                    qty_delta_units=delta_qty if delta_qty > 0 else 0.0,
                    refresh_recon=True,
                )

            elif effective_delta < -_EPS:
                # Negative — production may already have consumed some
                # of the existing total. Refuse to mutate; surface to
                # the operator so they can revise_order manually if
                # the reduction is intentional.
                warned += 1
                note = (
                    f"Incoming qty ({incoming_qty:.3f} units / {incoming_kg:.3f} kg) "
                    f"is below the on-file ordered total "
                    f"({existing_qty:.3f} units / {existing_kg:.3f} kg). "
                    "Line left unchanged — review fulfillment_v2 before reducing."
                )
                await _attach_gst_and_pack(
                    so_line_id=so_line_id,
                    line_number=existing["line_number"],
                    reconcile_status="warning",
                    reconcile_note=note,
                    qty_delta_kg=delta_kg,
                    qty_delta_units=delta_qty,
                    refresh_recon=False,
                )

            else:
                # Within tolerance — refresh metadata silently. No
                # fulfillment_v2 touch needed since the qty didn't move.
                await conn.execute(
                    """
                    UPDATE so_line SET
                        item_category = COALESCE($2, item_category),
                        sub_category = COALESCE($3, sub_category),
                        uom = COALESCE($4, uom),
                        grp_code = COALESCE($5, grp_code),
                        rate_inr = COALESCE($6, rate_inr),
                        rate_type = COALESCE($7, rate_type),
                        amount_inr = COALESCE($8, amount_inr),
                        total_amount_inr = COALESCE($9, total_amount_inr)
                    WHERE so_line_id = $1
                    """,
                    so_line_id,
                    row.get("item_category"),
                    row.get("sub_category"),
                    row.get("uom"),
                    row.get("grp_code"),
                    row.get("rate_inr"),
                    rate_type,
                    row.get("amount_inr"),
                    row.get("total_amount_inr"),
                )
                await _attach_gst_and_pack(
                    so_line_id=so_line_id,
                    line_number=existing["line_number"],
                    reconcile_status="unchanged",
                    reconcile_note=None,
                    qty_delta_kg=0.0,
                    qty_delta_units=0.0,
                    refresh_recon=True,
                )

        else:
            # Article wasn't on file under this SO — attach as a new
            # so_line. We bump max_line_number rather than re-deriving
            # from a counter so duplicate-SKU incoming rows after the
            # first one ("consumed" case above) land at unique
            # line_numbers.
            max_line_number += 1
            new_line_number = max_line_number

            async def _insert_new_line():
                return await conn.fetchrow(
                    """
                    INSERT INTO so_line (
                        so_id, line_number, sku_name, item_category, sub_category,
                        uom, grp_code, quantity, quantity_units, rate_inr,
                        amount_inr, igst_amount, sgst_amount, cgst_amount,
                        apmc_amount, packing_amount, freight_amount, processing_amount,
                        total_amount_inr, rate_type,
                        item_type, item_description, sales_group,
                        match_score, match_source, status, so_line_id
                    )
                    VALUES (
                        $1, $2, $3, $4, $5,
                        $6, $7, $8, $9, $10,
                        $11, $12, $13, $14,
                        $15, $16, $17, $18,
                        $19, $20,
                        $21, $22, $23,
                        $24, $25, 'pending', $26
                    )
                    RETURNING so_line_id
                    """,
                    so_id,
                    new_line_number,
                    sku_name,
                    row.get("item_category"),
                    row.get("sub_category"),
                    row.get("uom"),
                    row.get("grp_code"),
                    row.get("quantity"),
                    (int(round(computed_qty_units))
                     if computed_qty_units is not None else None),
                    row.get("rate_inr"),
                    row.get("amount_inr"),
                    row.get("igst_amount"),
                    row.get("sgst_amount"),
                    row.get("cgst_amount"),
                    row.get("apmc_amount", 0),
                    row.get("packing_amount", 0),
                    row.get("freight_amount", 0),
                    row.get("processing_amount", 0),
                    row.get("total_amount_inr"),
                    rate_type,
                    matched_item.item_type if matched_item else None,
                    matched_item.particulars if matched_item else None,
                    matched_item.sale_group if matched_item else None,
                    score if matched_item else None,
                    "all_sku" if matched_item else None,
                    new_short_time_id(),
                )

            line_row = await insert_with_pk_retry(conn, _insert_new_line)
            so_line_id = line_row["so_line_id"]
            added += 1
            await _attach_gst_and_pack(
                so_line_id=so_line_id,
                line_number=new_line_number,
                reconcile_status="new",
                reconcile_note="Added to existing SO",
                qty_delta_kg=_to_float(computed_qty_units),
                qty_delta_units=_to_float(row.get("quantity")),
                refresh_recon=True,
            )

    logger.info(
        "Reconciled SO '%s' (so_id=%s): %d new lines, %d bumped, %d warned, %d total incoming",
        so_number, so_id, added, bumped, warned, len(normalized_rows),
    )

    return {
        "lines": results,
        "matched_lines": matched_delta,
        "unmatched_lines": unmatched_delta,
        "gst_ok": gst_ok,
        "gst_mismatch": gst_mismatch,
        "gst_warning": gst_warning,
        "added_line_count": added,
        "qty_bumped_count": bumped,
        "qty_warning_count": warned,
    }
