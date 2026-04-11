"""SO update flow -- preview, confirm (Excel), and manual single-SO update."""

import hashlib
import logging
from datetime import date, datetime

from app.modules.so.services.parser import parse_sales_register
from app.modules.so.services.gst_reconciliation import reconcile_line
from app.modules.so.services.item_matcher import MasterItem, match_sku

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Update flow: preview + confirm
# ---------------------------------------------------------------------------

# Fields compared at header level
_HEADER_COMPARE_FIELDS = [
    "so_date", "customer_name", "common_customer_name", "company", "voucher_type",
]

# Fields compared at line level (matched by line_number)
_LINE_COMPARE_FIELDS = [
    "sku_name", "item_category", "sub_category", "uom", "grp_code",
    "quantity", "rate_inr", "amount_inr",
    "igst_amount", "sgst_amount", "cgst_amount",
    "apmc_amount", "packing_amount", "freight_amount", "processing_amount",
    "total_amount_inr",
]

# Numeric fields — normalised to 3 decimal places before comparing
_NUMERIC_FIELDS = {
    "quantity", "rate_inr", "amount_inr",
    "igst_amount", "sgst_amount", "cgst_amount",
    "apmc_amount", "packing_amount", "freight_amount", "processing_amount",
    "total_amount_inr",
}


def _norm(val, field: str) -> str | None:
    """Normalise a value to a comparable string representation."""
    if field in _NUMERIC_FIELDS:
        # Treat None and 0 as equivalent for numeric fields (DB NULL == 0)
        if val is None:
            return "0.0"
        try:
            return str(round(float(val), 3))
        except (ValueError, TypeError):
            return "0.0"
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _diff_fields(old: dict, new: dict, fields: list[str]) -> list[dict]:
    """Return list of {field, old_value, new_value} for fields that differ."""
    changes = []
    for f in fields:
        old_v = _norm(old.get(f), f)
        new_v = _norm(new.get(f), f)
        if old_v != new_v:
            changes.append({"field": f, "old_value": old_v, "new_value": new_v})
    return changes


def _db_header_to_dict(row) -> dict:
    """Convert an asyncpg Record for so_header into a comparable dict."""
    return {
        "so_date": str(row["so_date"]) if row.get("so_date") else None,
        "customer_name": row.get("customer_name"),
        "common_customer_name": row.get("common_customer_name"),
        "company": row.get("company"),
        "voucher_type": row.get("voucher_type"),
    }


def _db_line_to_dict(row) -> dict:
    """Convert an asyncpg Record for so_line into a comparable dict."""
    return {
        "so_line_id": row["so_line_id"],
        "line_number": row["line_number"],
        "sku_name": row.get("sku_name"),
        "item_category": row.get("item_category"),
        "sub_category": row.get("sub_category"),
        "uom": str(row["uom"]) if row.get("uom") is not None else None,
        "grp_code": row.get("grp_code"),
        "quantity": round(float(row["quantity"]), 3) if row.get("quantity") is not None else None,
        "rate_inr": round(float(row["rate_inr"]), 3) if row.get("rate_inr") is not None else None,
        "amount_inr": round(float(row["amount_inr"]), 3) if row.get("amount_inr") is not None else None,
        "igst_amount": round(float(row["igst_amount"]), 3) if row.get("igst_amount") is not None else None,
        "sgst_amount": round(float(row["sgst_amount"]), 3) if row.get("sgst_amount") is not None else None,
        "cgst_amount": round(float(row["cgst_amount"]), 3) if row.get("cgst_amount") is not None else None,
        "apmc_amount": round(float(row["apmc_amount"]), 3) if row.get("apmc_amount") is not None else None,
        "packing_amount": round(float(row["packing_amount"]), 3) if row.get("packing_amount") is not None else None,
        "freight_amount": round(float(row["freight_amount"]), 3) if row.get("freight_amount") is not None else None,
        "processing_amount": round(float(row["processing_amount"]), 3) if row.get("processing_amount") is not None else None,
        "total_amount_inr": round(float(row["total_amount_inr"]), 3) if row.get("total_amount_inr") is not None else None,
    }


def _excel_row_to_header_dict(first_row: dict, raw_date) -> dict:
    """Convert a parsed Excel first-row into a header dict for comparison."""
    from datetime import date, datetime
    so_date_str = None
    if raw_date:
        if isinstance(raw_date, datetime):
            so_date_str = raw_date.strftime("%Y-%m-%d")
        elif isinstance(raw_date, date):
            so_date_str = raw_date.isoformat()
    return {
        "so_date": so_date_str,
        "customer_name": first_row.get("customer_name"),
        "common_customer_name": first_row.get("common_customer_name"),
        "company": first_row.get("company"),
        "voucher_type": first_row.get("voucher_type"),
    }


def _excel_row_to_line_dict(row: dict, line_number: int) -> dict:
    """Convert a parsed Excel row into the same shape as _db_line_to_dict."""
    return {
        "line_number": line_number,
        "sku_name": row.get("article"),
        "item_category": row.get("item_category"),
        "sub_category": row.get("sub_category"),
        "uom": str(row["uom"]) if row.get("uom") is not None else None,
        "grp_code": row.get("grp_code"),
        "quantity": row.get("quantity"),
        "rate_inr": row.get("rate_inr"),
        "amount_inr": row.get("amount_inr"),
        "igst_amount": row.get("igst_amount"),
        "sgst_amount": row.get("sgst_amount"),
        "cgst_amount": row.get("cgst_amount"),
        "apmc_amount": row.get("apmc_amount"),
        "packing_amount": row.get("packing_amount"),
        "freight_amount": row.get("freight_amount"),
        "processing_amount": row.get("processing_amount"),
        "total_amount_inr": row.get("total_amount_inr"),
    }


async def preview_sales_register_update(pool, file_bytes: bytes) -> dict:
    """
    Parse an Excel file and compare against existing SOs in the DB.
    Returns only SOs that have changes, with a field-by-field diff.
    Includes a SHA-256 file_hash so the confirm step can verify the same
    file is being applied (Bug 4).
    """
    # Bug 4: Compute hash here so the confirm step can re-verify the bytes.
    file_hash = hashlib.sha256(file_bytes).hexdigest()

    groups = parse_sales_register(file_bytes)

    not_found = []
    unchanged_count = 0
    changes = []

    async with pool.acquire() as conn:
        for so_number, rows in groups.items():
            header = await conn.fetchrow(
                "SELECT * FROM so_header WHERE so_number = $1", so_number
            )
            if not header:
                not_found.append(so_number)
                continue

            so_id = header["so_id"]
            first_row = rows[0]

            current_header = _db_header_to_dict(header)
            new_header = _excel_row_to_header_dict(first_row, first_row.get("date"))

            header_changes = _diff_fields(current_header, new_header, _HEADER_COMPARE_FIELDS)

            # Fetch existing lines
            db_lines = await conn.fetch(
                "SELECT * FROM so_line WHERE so_id = $1 ORDER BY line_number", so_id
            )
            current_lines = [_db_line_to_dict(l) for l in db_lines]
            new_lines = [_excel_row_to_line_dict(r, i) for i, r in enumerate(rows, 1)]

            current_by_ln = {l["line_number"]: l for l in current_lines}

            line_changes = []

            for nl in new_lines:
                ln = nl["line_number"]
                cl = current_by_ln.pop(ln, None)
                if cl is None:
                    line_changes.append({
                        "line_number": ln,
                        "sku_name": nl.get("sku_name"),
                        "change_type": "added",
                        "changes": [
                            {"field": f, "old_value": None, "new_value": _norm(nl.get(f), f)}
                            for f in _LINE_COMPARE_FIELDS
                            if _norm(nl.get(f), f) is not None
                        ],
                    })
                else:
                    field_changes = _diff_fields(cl, nl, _LINE_COMPARE_FIELDS)
                    if field_changes:
                        line_changes.append({
                            "line_number": ln,
                            "sku_name": nl.get("sku_name") or cl.get("sku_name"),
                            "change_type": "modified",
                            "changes": field_changes,
                        })

            # Lines in DB but not in Excel — removed
            for ln, cl in current_by_ln.items():
                line_changes.append({
                    "line_number": ln,
                    "sku_name": cl.get("sku_name"),
                    "change_type": "removed",
                    "changes": [
                        {"field": f, "old_value": _norm(cl.get(f), f), "new_value": None}
                        for f in _LINE_COMPARE_FIELDS
                        if _norm(cl.get(f), f) is not None
                    ],
                })

            if not header_changes and not line_changes:
                unchanged_count += 1
                continue

            changes.append({
                "so_id": so_id,
                "so_number": so_number,
                "header_changes": header_changes,
                "line_changes": line_changes,
                "current_header": current_header,
                "new_header": new_header,
                "current_lines": current_lines,
                "new_lines": new_lines,
            })

    logger.info(
        "Update preview: %d in file, %d changed, %d unchanged, %d not found",
        len(groups), len(changes), unchanged_count, len(not_found),
    )

    return {
        "total_in_file": len(groups),
        "unchanged_count": unchanged_count,
        "changed_count": len(changes),
        "not_found_so_numbers": not_found,
        "changes": changes,
        # Bug 4: Return hash so the confirm endpoint can verify the same file.
        "file_hash": file_hash,
    }


async def confirm_sales_register_update(
    pool,
    so_ids: list[int],
    file_bytes: bytes,
    master_items: list[MasterItem],
    file_hash: str,
) -> dict:
    """
    Apply updates for the given so_ids from the Excel file.
    Deletes old lines + recon, re-inserts from Excel, re-runs matching + GST recon.
    Returns the updated SO details.
    """
    from datetime import date, datetime

    # Bug 4: Verify the caller is confirming the exact file that was previewed.
    computed_hash = hashlib.sha256(file_bytes).hexdigest()
    if computed_hash != file_hash:
        raise ValueError(
            "File mismatch: the uploaded file does not match the previewed file. "
            "Please re-run the preview with the correct file."
        )

    groups = parse_sales_register(file_bytes)

    # Map approved so_ids to so_numbers
    async with pool.acquire() as conn:
        approved = await conn.fetch(
            "SELECT so_id, so_number FROM so_header WHERE so_id = ANY($1)", so_ids
        )
    approved_map = {r["so_number"]: r["so_id"] for r in approved}

    all_so_details = []
    total_lines = 0
    total_matched = 0
    total_unmatched = 0
    total_gst_ok = 0
    total_gst_mismatch = 0
    total_gst_warning = 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            for so_number, rows in groups.items():
                so_id = approved_map.get(so_number)
                if so_id is None:
                    continue

                first_row = rows[0]

                so_date = None
                raw_date = first_row.get("date")
                if raw_date:
                    if isinstance(raw_date, datetime):
                        so_date = raw_date.date()
                    elif isinstance(raw_date, date):
                        so_date = raw_date

                # Update header
                await conn.execute(
                    """
                    UPDATE so_header SET
                        so_date = $2, customer_name = $3, common_customer_name = $4,
                        company = $5, voucher_type = $6
                    WHERE so_id = $1
                    """,
                    so_id, so_date,
                    first_row.get("customer_name"),
                    first_row.get("common_customer_name"),
                    first_row.get("company"),
                    first_row.get("voucher_type"),
                )

                # Delete old recon + lines (order matters — FK constraints).
                # so_gst_reconciliation.so_line_id → so_line, so_fulfillment.so_line_id → so_line.
                await conn.execute(
                    "DELETE FROM so_gst_reconciliation WHERE so_id = $1", so_id
                )
                # Bug 3: so_fulfillment has a FK on so_line_id; delete it first or
                # the DELETE FROM so_line will raise a FK violation.
                await conn.execute(
                    "DELETE FROM so_fulfillment WHERE so_id = $1", so_id
                )
                await conn.execute(
                    "DELETE FROM so_line WHERE so_id = $1", so_id
                )

                # Re-insert lines with matching + GST recon
                so_lines_with_recon = []
                so_gst_ok = 0
                so_gst_mismatch = 0
                so_gst_warning = 0

                for line_idx, row in enumerate(rows, start=1):
                    article = row.get("article")

                    matched_item, score = match_sku(article, master_items) if article else (None, 0.0)

                    quantity_units = None
                    rate_type = None
                    if matched_item and matched_item.uom is not None:
                        rate_type = "per_kg" if matched_item.uom == 1 else "per_unit"
                        qty = row.get("quantity")
                        if qty is not None and matched_item.uom > 0:
                            # Bug 5: round() preserves fractional units; int() silently truncates.
                            quantity_units = round(qty * matched_item.uom, 3)

                    if matched_item:
                        total_matched += 1
                    else:
                        total_unmatched += 1

                    line_row = await conn.fetchrow(
                        """
                        INSERT INTO so_line (
                            so_id, line_number, sku_name, item_category, sub_category,
                            uom, grp_code, quantity, quantity_units, rate_inr,
                            amount_inr, igst_amount, sgst_amount, cgst_amount,
                            apmc_amount, packing_amount, freight_amount, processing_amount,
                            total_amount_inr, rate_type,
                            item_type, item_description, sales_group,
                            match_score, match_source, status
                        )
                        VALUES (
                            $1, $2, $3, $4, $5,
                            $6, $7, $8, $9, $10,
                            $11, $12, $13, $14,
                            $15, $16, $17, $18,
                            $19, $20,
                            $21, $22, $23,
                            $24, $25, 'pending'::e_so_line_status
                        )
                        RETURNING so_line_id
                        """,
                        so_id,
                        line_idx,
                        article,
                        row.get("item_category"),
                        row.get("sub_category"),
                        str(row.get("uom")) if row.get("uom") is not None else None,
                        row.get("grp_code"),
                        row.get("quantity"),
                        quantity_units,
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
                    )
                    so_line_id = line_row["so_line_id"]
                    total_lines += 1

                    recon = reconcile_line(row, matched_item)
                    recon["match_score"] = score if matched_item else None

                    if recon["status"] == "ok":
                        so_gst_ok += 1
                    elif recon["status"] == "mismatch":
                        so_gst_mismatch += 1
                    else:
                        so_gst_warning += 1

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

                    so_lines_with_recon.append({
                        "line": {
                            "so_line_id": so_line_id,
                            "line_number": line_idx,
                            "sku_name": article,
                            "item_category": row.get("item_category"),
                            "sub_category": row.get("sub_category"),
                            "uom": str(row.get("uom")) if row.get("uom") is not None else None,
                            "grp_code": row.get("grp_code"),
                            "quantity": row.get("quantity"),
                            "quantity_units": quantity_units,
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
                            "line_number": line_idx,
                            "sku_name": article,
                            **{k: recon[k] for k in recon},
                        },
                    })

                total_gst_ok += so_gst_ok
                total_gst_mismatch += so_gst_mismatch
                total_gst_warning += so_gst_warning

                all_so_details.append({
                    "so_id": so_id,
                    "so_number": so_number,
                    "so_date": str(so_date) if so_date else None,
                    "customer_name": first_row.get("customer_name"),
                    "common_customer_name": first_row.get("common_customer_name"),
                    "company": first_row.get("company"),
                    "voucher_type": first_row.get("voucher_type"),
                    "total_lines": len(rows),
                    "gst_ok": so_gst_ok,
                    "gst_mismatch": so_gst_mismatch,
                    "gst_warning": so_gst_warning,
                    "lines": so_lines_with_recon,
                })

    logger.info(
        "Update confirmed: %d SOs, %d lines (%d matched, %d unmatched)",
        len(all_so_details), total_lines, total_matched, total_unmatched,
    )

    return {
        "updated_count": len(all_so_details),
        "updated_so_numbers": [s["so_number"] for s in all_so_details],
        "sales_orders": all_so_details,
    }


async def manual_update_so(
    pool,
    so_data: dict,
    master_items: list[MasterItem],
) -> dict:
    """
    Update a single SO from manual JSON input.
    Validates old state matches DB (stale check), computes diff,
    then replaces header + lines + recon with new data.
    """
    from datetime import date as date_type

    so_number = so_data["so_number"]
    old_header = so_data["old_header"]
    new_header = so_data["new_header"]
    old_lines = so_data["old_lines"]
    new_lines = so_data["new_lines"]

    async with pool.acquire() as conn:
        # Find existing SO
        header = await conn.fetchrow(
            "SELECT * FROM so_header WHERE so_number = $1", so_number
        )
        if not header:
            raise ValueError(f"SO '{so_number}' not found in database.")

        so_id = header["so_id"]

        # --- Stale check: verify old state matches DB ---
        db_header = _db_header_to_dict(header)
        for field in _HEADER_COMPARE_FIELDS:
            db_val = _norm(db_header.get(field), field)
            old_val = _norm(old_header.get(field), field)
            if db_val != old_val:
                raise ValueError(
                    f"Stale data: header field '{field}' has changed since you loaded it. "
                    f"DB={db_val}, your old={old_val}. Please reload and try again."
                )

        db_lines_raw = await conn.fetch(
            "SELECT * FROM so_line WHERE so_id = $1 ORDER BY line_number", so_id
        )
        db_lines = [_db_line_to_dict(l) for l in db_lines_raw]
        db_by_ln = {l["line_number"]: l for l in db_lines}

        for ol in old_lines:
            ln = ol["line_number"]
            db_l = db_by_ln.get(ln)
            if db_l is None:
                # Bug 7: A missing line means someone else deleted it while the user had it open.
                # Silently skipping would hide the inconsistency; raise so the client reloads.
                raise ValueError(
                    f"Line {ln} was deleted externally. Please reload the SO before updating."
                )
            for field in _LINE_COMPARE_FIELDS:
                db_val = _norm(db_l.get(field), field)
                old_val = _norm(ol.get(field), field)
                if db_val != old_val:
                    raise ValueError(
                        f"Stale data: line {ln} field '{field}' has changed. "
                        f"DB={db_val}, your old={old_val}. Please reload and try again."
                    )

        # --- Compute diff for response ---
        header_changes = _diff_fields(
            {k: old_header.get(k) for k in _HEADER_COMPARE_FIELDS},
            {k: new_header.get(k) for k in _HEADER_COMPARE_FIELDS},
            _HEADER_COMPARE_FIELDS,
        )

        old_by_ln = {l["line_number"]: l for l in old_lines}
        line_changes = []

        for nl in new_lines:
            ln = nl["line_number"]
            ol = old_by_ln.pop(ln, None)
            if ol is None:
                line_changes.append({
                    "line_number": ln,
                    "sku_name": nl.get("sku_name"),
                    "change_type": "added",
                    "changes": [
                        {"field": f, "old_value": None, "new_value": _norm(nl.get(f), f)}
                        for f in _LINE_COMPARE_FIELDS
                        if _norm(nl.get(f), f) is not None
                    ],
                })
            else:
                field_changes = _diff_fields(ol, nl, _LINE_COMPARE_FIELDS)
                if field_changes:
                    line_changes.append({
                        "line_number": ln,
                        "sku_name": nl.get("sku_name") or ol.get("sku_name"),
                        "change_type": "modified",
                        "changes": field_changes,
                    })

        for ln, ol in old_by_ln.items():
            line_changes.append({
                "line_number": ln,
                "sku_name": ol.get("sku_name"),
                "change_type": "removed",
                "changes": [
                    {"field": f, "old_value": _norm(ol.get(f), f), "new_value": None}
                    for f in _LINE_COMPARE_FIELDS
                    if _norm(ol.get(f), f) is not None
                ],
            })

        # --- Apply update ---
        so_date = None
        if new_header.get("so_date"):
            try:
                so_date = date_type.fromisoformat(new_header["so_date"])
            except (ValueError, TypeError):
                pass

        async with conn.transaction():
            await conn.execute(
                """
                UPDATE so_header SET
                    so_date = $2, customer_name = $3, common_customer_name = $4,
                    company = $5, voucher_type = $6
                WHERE so_id = $1
                """,
                so_id, so_date,
                new_header.get("customer_name"),
                new_header.get("common_customer_name"),
                new_header.get("company"),
                new_header.get("voucher_type"),
            )

            await conn.execute(
                "DELETE FROM so_gst_reconciliation WHERE so_id = $1", so_id
            )
            await conn.execute(
                "DELETE FROM so_line WHERE so_id = $1", so_id
            )

            so_lines_with_recon = []
            so_gst_ok = 0
            so_gst_mismatch = 0
            so_gst_warning = 0
            total_matched = 0
            total_unmatched = 0

            for line_idx, nl in enumerate(new_lines, start=1):
                article = nl.get("sku_name")

                matched_item, score = match_sku(article, master_items) if article else (None, 0.0)

                quantity_units = nl.get("quantity_units")
                rate_type = None
                if matched_item and matched_item.uom is not None:
                    rate_type = "per_kg" if matched_item.uom == 1 else "per_unit"

                if matched_item:
                    total_matched += 1
                else:
                    total_unmatched += 1

                line_row = await conn.fetchrow(
                    """
                    INSERT INTO so_line (
                        so_id, line_number, sku_name, item_category, sub_category,
                        uom, grp_code, quantity, quantity_units, rate_inr,
                        amount_inr, igst_amount, sgst_amount, cgst_amount,
                        apmc_amount, packing_amount, freight_amount, processing_amount,
                        total_amount_inr, rate_type,
                        item_type, item_description, sales_group,
                        match_score, match_source, status
                    )
                    VALUES (
                        $1, $2, $3, $4, $5,
                        $6, $7, $8, $9, $10,
                        $11, $12, $13, $14,
                        $15, $16, $17, $18,
                        $19, $20,
                        $21, $22, $23,
                        $24, $25, 'pending'::e_so_line_status
                    )
                    RETURNING so_line_id
                    """,
                    so_id,
                    nl.get("line_number", line_idx),
                    article,
                    nl.get("item_category"),
                    nl.get("sub_category"),
                    nl.get("uom"),
                    nl.get("grp_code"),
                    nl.get("quantity"),
                    quantity_units,
                    nl.get("rate_inr"),
                    nl.get("amount_inr"),
                    nl.get("igst_amount", 0),
                    nl.get("sgst_amount", 0),
                    nl.get("cgst_amount", 0),
                    nl.get("apmc_amount", 0),
                    nl.get("packing_amount", 0),
                    nl.get("freight_amount", 0),
                    nl.get("processing_amount", 0),
                    nl.get("total_amount_inr"),
                    rate_type,
                    matched_item.item_type if matched_item else None,
                    matched_item.particulars if matched_item else None,
                    matched_item.sale_group if matched_item else None,
                    score if matched_item else None,
                    "all_sku" if matched_item else None,
                )
                so_line_id = line_row["so_line_id"]

                recon_input = {
                    "amount_inr": nl.get("amount_inr"),
                    "igst_amount": nl.get("igst_amount"),
                    "sgst_amount": nl.get("sgst_amount"),
                    "cgst_amount": nl.get("cgst_amount"),
                    "apmc_amount": nl.get("apmc_amount"),
                    "packing_amount": nl.get("packing_amount"),
                    "freight_amount": nl.get("freight_amount"),
                    "processing_amount": nl.get("processing_amount"),
                    "total_amount_inr": nl.get("total_amount_inr"),
                    "uom": nl.get("uom"),
                }
                recon = reconcile_line(recon_input, matched_item)
                recon["match_score"] = score if matched_item else None

                if recon["status"] == "ok":
                    so_gst_ok += 1
                elif recon["status"] == "mismatch":
                    so_gst_mismatch += 1
                else:
                    so_gst_warning += 1

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

                so_lines_with_recon.append({
                    "line": {
                        "so_line_id": so_line_id,
                        "line_number": nl.get("line_number", line_idx),
                        "sku_name": article,
                        "item_category": nl.get("item_category"),
                        "sub_category": nl.get("sub_category"),
                        "uom": nl.get("uom"),
                        "grp_code": nl.get("grp_code"),
                        "quantity": nl.get("quantity"),
                        "quantity_units": quantity_units,
                        "rate_inr": nl.get("rate_inr"),
                        "rate_type": rate_type,
                        "amount_inr": nl.get("amount_inr"),
                        "igst_amount": nl.get("igst_amount"),
                        "sgst_amount": nl.get("sgst_amount"),
                        "cgst_amount": nl.get("cgst_amount"),
                        "total_amount_inr": nl.get("total_amount_inr"),
                        "apmc_amount": nl.get("apmc_amount"),
                        "packing_amount": nl.get("packing_amount"),
                        "freight_amount": nl.get("freight_amount"),
                        "processing_amount": nl.get("processing_amount"),
                        "item_type": matched_item.item_type if matched_item else None,
                        "item_description": matched_item.particulars if matched_item else None,
                        "sales_group": matched_item.sale_group if matched_item else None,
                        "match_score": score if matched_item else None,
                        "match_source": "all_sku" if matched_item else None,
                        "status": "pending",
                    },
                    "gst_recon": {
                        "so_line_id": so_line_id,
                        "line_number": nl.get("line_number", line_idx),
                        "sku_name": article,
                        **{k: recon[k] for k in recon},
                    },
                })

    so_detail = {
        "so_id": so_id,
        "so_number": so_number,
        "so_date": str(so_date) if so_date else None,
        "customer_name": new_header.get("customer_name"),
        "common_customer_name": new_header.get("common_customer_name"),
        "company": new_header.get("company"),
        "voucher_type": new_header.get("voucher_type"),
        "total_lines": len(new_lines),
        "gst_ok": so_gst_ok,
        "gst_mismatch": so_gst_mismatch,
        "gst_warning": so_gst_warning,
        "lines": so_lines_with_recon,
    }

    logger.info(
        "Manual SO update: so_id=%d, %d lines (%d matched, %d unmatched), "
        "%d header changes, %d line changes",
        so_id, len(new_lines), total_matched, total_unmatched,
        len(header_changes), len(line_changes),
    )

    return {
        "so_id": so_id,
        "so_number": so_number,
        "header_changes": header_changes,
        "line_changes": line_changes,
        "sales_order": so_detail,
    }
