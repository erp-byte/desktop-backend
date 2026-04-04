"""SO ingestion — create SO header + lines + GST reconciliation from parsed data."""

import logging
from datetime import date, datetime

from app.core.helpers import safe_float
from app.modules.so.services.gst_reconciliation import reconcile_line
from app.modules.so.services.item_matcher import MasterItem, match_sku
from app.modules.so.services.so_book_parser import parse_so_book
from app.modules.so.services.parser import parse_sales_register

logger = logging.getLogger(__name__)


async def ingest_sales_register(
    pool,
    file_bytes: bytes,
    master_items: list[MasterItem],
) -> dict:
    """
    Parse Excel, create so_header + so_line rows, run GST reconciliation.
    Returns summary dict.
    """
    groups = parse_sales_register(file_bytes)

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
                first_row = rows[0]

                # Parse date
                so_date = None
                raw_date = first_row.get("date")
                if raw_date:
                    if isinstance(raw_date, datetime):
                        so_date = raw_date.date()
                    elif isinstance(raw_date, date):
                        so_date = raw_date

                # INSERT so_header
                header = await conn.fetchrow(
                    """
                    INSERT INTO so_header (
                        so_number, so_date, customer_name, common_customer_name,
                        company, voucher_type, extraction_status
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, 'extracted'::e_extraction_status)
                    RETURNING so_id
                    """,
                    so_number,
                    so_date,
                    first_row.get("customer_name"),
                    first_row.get("common_customer_name"),
                    first_row.get("company"),
                    first_row.get("voucher_type"),
                )
                so_id = header["so_id"]

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
                            quantity_units = int(qty * matched_item.uom)

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
        "Ingested %d SOs, %d lines (%d matched, %d unmatched), GST: %d ok, %d mismatch, %d warning",
        len(all_so_details), total_lines, total_matched, total_unmatched,
        total_gst_ok, total_gst_mismatch, total_gst_warning,
    )

    return {
        "summary": {
            "total_sos": len(all_so_details),
            "total_lines": total_lines,
            "matched_lines": total_matched,
            "unmatched_lines": total_unmatched,
            "gst_ok": total_gst_ok,
            "gst_mismatch": total_gst_mismatch,
            "gst_warning": total_gst_warning,
        },
        "sales_orders": all_so_details,
    }


async def ingest_manual_so(
    pool,
    so_data: dict,
    master_items: list[MasterItem],
) -> dict:
    """
    Create a single SO from manual JSON input.
    Uses the same matching + reconciliation logic as Excel ingestion.
    Returns same response shape as ingest_sales_register.
    """
    so_number = so_data["so_number"]
    lines_input = so_data["lines"]

    # Convert to the same row_data format as Excel parsing produces
    rows = []
    for line in lines_input:
        rows.append({
            "article": line.get("sku_name"),
            "item_category": line.get("item_category"),
            "sub_category": line.get("sub_category"),
            "uom": safe_float(line.get("uom")),
            "grp_code": line.get("grp_code"),
            "quantity": line.get("quantity"),
            "quantity_units": line.get("quantity_units"),
            "rate_inr": line.get("rate_inr"),
            "amount_inr": line.get("amount_inr"),
            "igst_amount": line.get("igst_amount") or 0,
            "sgst_amount": line.get("sgst_amount") or 0,
            "cgst_amount": line.get("cgst_amount") or 0,
            "apmc_amount": line.get("apmc_amount") or 0,
            "packing_amount": line.get("packing_amount") or 0,
            "freight_amount": line.get("freight_amount") or 0,
            "processing_amount": line.get("processing_amount") or 0,
            "total_amount_inr": line.get("total_amount_inr"),
        })

    # Parse date
    so_date = None
    raw_date = so_data.get("so_date")
    if raw_date:
        try:
            so_date = date.fromisoformat(raw_date)
        except (ValueError, TypeError):
            pass

    total_matched = 0
    total_unmatched = 0
    total_gst_ok = 0
    total_gst_mismatch = 0
    total_gst_warning = 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            header = await conn.fetchrow(
                """
                INSERT INTO so_header (
                    so_number, so_date, customer_name, common_customer_name,
                    company, voucher_type, extraction_status
                )
                VALUES ($1, $2, $3, $4, $5, $6, 'extracted'::e_extraction_status)
                RETURNING so_id
                """,
                so_number,
                so_date,
                so_data.get("customer_name"),
                so_data.get("common_customer_name"),
                so_data.get("company"),
                so_data.get("voucher_type"),
            )
            so_id = header["so_id"]

            so_lines_with_recon = []

            for line_idx, row in enumerate(rows, start=1):
                article = row.get("article")

                matched_item, score = match_sku(article, master_items) if article else (None, 0.0)

                quantity_units = row.get("quantity_units")
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

                recon = reconcile_line(row, matched_item)
                recon["match_score"] = score if matched_item else None

                if recon["status"] == "ok":
                    total_gst_ok += 1
                elif recon["status"] == "mismatch":
                    total_gst_mismatch += 1
                else:
                    total_gst_warning += 1

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

    so_detail = {
        "so_id": so_id,
        "so_number": so_number,
        "so_date": str(so_date) if so_date else None,
        "customer_name": so_data.get("customer_name"),
        "common_customer_name": so_data.get("common_customer_name"),
        "company": so_data.get("company"),
        "voucher_type": so_data.get("voucher_type"),
        "total_lines": len(rows),
        "gst_ok": total_gst_ok,
        "gst_mismatch": total_gst_mismatch,
        "gst_warning": total_gst_warning,
        "lines": so_lines_with_recon,
    }

    logger.info(
        "Manual SO created: so_id=%d, %d lines (%d matched, %d unmatched), GST: %d ok, %d mismatch, %d warning",
        so_id, len(rows), total_matched, total_unmatched,
        total_gst_ok, total_gst_mismatch, total_gst_warning,
    )

    return {
        "summary": {
            "total_sos": 1,
            "total_lines": len(rows),
            "matched_lines": total_matched,
            "unmatched_lines": total_unmatched,
            "gst_ok": total_gst_ok,
            "gst_mismatch": total_gst_mismatch,
            "gst_warning": total_gst_warning,
        },
        "sales_orders": [so_detail],
    }


async def ingest_so_book(
    pool,
    file_bytes: bytes,
    master_items: list[MasterItem],
) -> dict:
    """
    Parse a Sales Order Book Excel, create so_header + so_line rows, run GST reconciliation.
    Same response shape as ingest_sales_register.
    """
    parsed_orders = parse_so_book(file_bytes)

    all_so_details = []
    total_lines = 0
    total_matched = 0
    total_unmatched = 0
    total_gst_ok = 0
    total_gst_mismatch = 0
    total_gst_warning = 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            for so in parsed_orders:
                so_date = None
                if so.get("so_date"):
                    try:
                        so_date = date.fromisoformat(so["so_date"])
                    except (ValueError, TypeError):
                        pass

                header = await conn.fetchrow(
                    """
                    INSERT INTO so_header (
                        so_number, so_date, customer_name, common_customer_name,
                        company, voucher_type, extraction_status
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, 'extracted'::e_extraction_status)
                    RETURNING so_id
                    """,
                    so["so_number"],
                    so_date,
                    so.get("customer_name"),
                    so.get("common_customer_name"),
                    so.get("company"),
                    so.get("voucher_type"),
                )
                so_id = header["so_id"]

                so_lines_with_recon = []
                so_gst_ok = 0
                so_gst_mismatch = 0
                so_gst_warning = 0

                for line in so.get("lines", []):
                    article = line.get("sku_name")

                    matched_item, score = match_sku(article, master_items) if article else (None, 0.0)

                    quantity_units = None
                    rate_type = None
                    if matched_item and matched_item.uom is not None:
                        rate_type = "per_kg" if matched_item.uom == 1 else "per_unit"
                        qty = line.get("quantity")
                        if qty is not None and matched_item.uom > 0:
                            quantity_units = int(qty * matched_item.uom)

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
                        line.get("line_number", 1),
                        article,
                        matched_item.group if matched_item else None,
                        matched_item.sub_group if matched_item else None,
                        line.get("uom"),
                        None,  # grp_code — not in SO Book format
                        line.get("quantity"),
                        quantity_units,
                        line.get("rate_inr"),
                        line.get("amount_inr"),
                        line.get("igst_amount", 0),
                        line.get("sgst_amount", 0),
                        line.get("cgst_amount", 0),
                        0,  # apmc — not in SO Book
                        0,  # packing — apportioned at header, not per line
                        0,  # freight
                        0,  # processing
                        line.get("total_amount_inr"),
                        rate_type,
                        matched_item.item_type if matched_item else None,
                        matched_item.particulars if matched_item else None,
                        matched_item.sale_group if matched_item else None,
                        score if matched_item else None,
                        "all_sku" if matched_item else None,
                    )
                    so_line_id = line_row["so_line_id"]
                    total_lines += 1

                    # GST reconciliation
                    recon_input = {
                        "amount_inr": line.get("amount_inr"),
                        "igst_amount": line.get("igst_amount", 0),
                        "sgst_amount": line.get("sgst_amount", 0),
                        "cgst_amount": line.get("cgst_amount", 0),
                        "apmc_amount": 0,
                        "packing_amount": 0,
                        "freight_amount": 0,
                        "processing_amount": 0,
                        "total_amount_inr": line.get("total_amount_inr"),
                        "uom": line.get("uom"),
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
                            "line_number": line.get("line_number", 1),
                            "sku_name": article,
                            "item_category": matched_item.group if matched_item else None,
                            "sub_category": matched_item.sub_group if matched_item else None,
                            "uom": line.get("uom"),
                            "grp_code": None,
                            "quantity": line.get("quantity"),
                            "quantity_units": quantity_units,
                            "rate_inr": line.get("rate_inr"),
                            "rate_type": rate_type,
                            "amount_inr": line.get("amount_inr"),
                            "igst_amount": line.get("igst_amount"),
                            "sgst_amount": line.get("sgst_amount"),
                            "cgst_amount": line.get("cgst_amount"),
                            "total_amount_inr": line.get("total_amount_inr"),
                            "apmc_amount": None,
                            "packing_amount": None,
                            "freight_amount": None,
                            "processing_amount": None,
                            "item_type": matched_item.item_type if matched_item else None,
                            "item_description": matched_item.particulars if matched_item else None,
                            "sales_group": matched_item.sale_group if matched_item else None,
                            "match_score": score if matched_item else None,
                            "match_source": "all_sku" if matched_item else None,
                            "status": "pending",
                        },
                        "gst_recon": {
                            "so_line_id": so_line_id,
                            "line_number": line.get("line_number", 1),
                            "sku_name": article,
                            **{k: recon[k] for k in recon},
                        },
                    })

                total_gst_ok += so_gst_ok
                total_gst_mismatch += so_gst_mismatch
                total_gst_warning += so_gst_warning

                all_so_details.append({
                    "so_id": so_id,
                    "so_number": so["so_number"],
                    "so_date": so.get("so_date"),
                    "customer_name": so.get("customer_name"),
                    "common_customer_name": so.get("common_customer_name"),
                    "company": so.get("company"),
                    "voucher_type": so.get("voucher_type"),
                    "total_lines": len(so.get("lines", [])),
                    "gst_ok": so_gst_ok,
                    "gst_mismatch": so_gst_mismatch,
                    "gst_warning": so_gst_warning,
                    "lines": so_lines_with_recon,
                })

    logger.info(
        "SO Book ingested: %d SOs, %d lines (%d matched, %d unmatched), GST: %d ok, %d mismatch, %d warning",
        len(all_so_details), total_lines, total_matched, total_unmatched,
        total_gst_ok, total_gst_mismatch, total_gst_warning,
    )

    return {
        "summary": {
            "total_sos": len(all_so_details),
            "total_lines": total_lines,
            "matched_lines": total_matched,
            "unmatched_lines": total_unmatched,
            "gst_ok": total_gst_ok,
            "gst_mismatch": total_gst_mismatch,
            "gst_warning": total_gst_warning,
        },
        "sales_orders": all_so_details,
    }
