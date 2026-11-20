"""SO Creation's second upload: the Tally Sales Register (invoices raised).

process_sales_invoices reads the file (sales_invoice_parser) and, without
writing anything:
  * links each invoice to its SO: the Voucher Ref. No. looked up in
    so_header.so_number (CF-SO/26-27/1404, and also SOs numbered with the
    customer's PO number such as 8700942817 or PO/00852);
  * links each item to the SO line it fulfils (article matched within that SO);
  * matches each item to the master list and works out its kg;
  * checks the file against itself (voucher value = its items, file total =
    the Grand Total row, no voucher number twice).

save_sales_invoices then writes the result to sales_invoice /
sales_invoice_line (migration 141): one row per invoice number (re-uploading
an invoice updates it), so_number referencing so_header, so one SO can have
many invoices.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

from rapidfuzz import fuzz, process

from app.modules.so.services.item_matcher import MasterItem, match_sku
from app.modules.so.services.sales_invoice_parser import parse_sales_invoices

# A master-list match below this is shown as "check the article".
CONFIDENT_MATCH = 0.90
# An invoice item is tied to an SO line of its SO at or above this (0-100).
# Checked 19 Nov 2026: 1,200 of 1,204 items on linked invoices scored 100.
SO_LINE_MATCH = 85
# Rupee slack when comparing totals.
VALUE_TOLERANCE = 1.0


class InvoiceTablesMissing(Exception):
    """sales_invoice / sales_invoice_line don't exist yet (migration 141)."""


def _line_kg(line: dict[str, Any], item: MasterItem | None) -> tuple[float | None, str | None]:
    """kg of an item line: Tally's Alt. Units (kg) when given, else quantity x
    the article's kg per unit from the master list."""
    if line["alt_units"] is not None:
        return line["alt_units"], "alt_units"
    if item is not None and item.uom and item.uom > 0 and line["quantity"] is not None:
        return round(line["quantity"] * item.uom, 3), "master_uom"
    return None, None


async def _load_sos(pool, references: list[str]) -> dict[str, dict[str, Any]]:
    """Upper-cased reference -> {so_number, lines: [{so_line_id, sku_name}]} for
    every reference that is an SO number in so_header."""
    if not references or pool is None:
        return {}
    async with pool.acquire() as conn:
        heads = await conn.fetch(
            "SELECT so_id, so_number FROM so_header WHERE upper(trim(so_number)) = ANY($1::text[])",
            references,
        )
        if not heads:
            return {}
        rows = await conn.fetch(
            "SELECT so_id, so_line_id, sku_name FROM so_line WHERE so_id = ANY($1::int[]) "
            "ORDER BY so_id, line_number",
            [h["so_id"] for h in heads],
        )
    lines: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        lines[r["so_id"]].append({"so_line_id": r["so_line_id"], "sku_name": r["sku_name"] or ""})
    return {h["so_number"].strip().upper(): {"so_number": h["so_number"], "lines": lines[h["so_id"]]}
            for h in heads}


def _so_line_for(particulars: str, so_lines: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float]:
    if not so_lines:
        return None, 0.0
    hit = process.extractOne(particulars.lower(), [ln["sku_name"].lower() for ln in so_lines],
                             scorer=fuzz.token_sort_ratio, score_cutoff=SO_LINE_MATCH)
    return (so_lines[hit[2]], hit[1]) if hit else (None, 0.0)


async def process_sales_invoices(pool, file_bytes: bytes, master_items: list[MasterItem]) -> dict[str, Any]:
    parsed = parse_sales_invoices(file_bytes)
    invoices = parsed["invoices"]

    names = sorted({ln["particulars"] for inv in invoices for ln in inv["lines"]})
    matches = {n: match_sku(n, master_items) for n in names}

    refs = sorted({(inv["reference"] or "").strip().upper() for inv in invoices if inv["reference"]})
    sos = await _load_sos(pool, refs)

    issues: list[str] = []
    seen = Counter(inv["voucher_no"] for inv in invoices)
    duplicates = sorted(no for no, n in seen.items() if n > 1)
    for no in duplicates:
        issues.append(f"Voucher {no} appears {seen[no]} times in the file.")
    if parsed["orphan_lines"]:
        issues.append(f"{parsed['orphan_lines']} item row(s) came before any voucher and were skipped.")

    unmatched: Counter[str] = Counter()
    low: Counter[str] = Counter()
    by_type: Counter[str] = Counter()
    by_site: Counter[str] = Counter()
    total_value = total_gross = total_kg = 0.0
    lines_without_kg = line_count = lines_on_so = lines_not_on_so = 0
    file_value = 0.0

    for inv in invoices:
        inv_issues: list[str] = []
        file_value += inv["value"] or 0.0
        so = sos.get((inv["reference"] or "").strip().upper())
        inv["so_number"] = so["so_number"] if so else None
        inv["so_found"] = so is not None
        if inv["quoted_so_number"] and so is None:
            inv_issues.append(f"SO {inv['quoted_so_number']} is not in the ERP.")
        for ln in inv["lines"]:
            item, score = matches.get(ln["particulars"], (None, 0.0))
            ln["matched_article"] = item.particulars if item else None
            ln["match_score"] = round(score, 3)
            ln["item_type"] = item.item_type if item else None
            ln["kg"], ln["kg_source"] = _line_kg(ln, item)
            so_line, so_score = _so_line_for(ln["particulars"], so["lines"]) if so else (None, 0.0)
            ln["so_line_id"] = so_line["so_line_id"] if so_line else None
            ln["so_line_article"] = so_line["sku_name"] if so_line else None
            ln["so_line_score"] = round(so_score / 100.0, 3)
            if item is None:
                unmatched[ln["particulars"]] += 1
            elif score < CONFIDENT_MATCH:
                low[ln["particulars"]] += 1
            if so and not inv["cancelled"]:
                if so_line:
                    lines_on_so += 1
                else:
                    lines_not_on_so += 1
                    inv_issues.append(f"Line {ln['line_no']} ({ln['particulars']}) is not on SO {so['so_number']}.")
        if inv["lines"] and inv["value"] is not None:
            lines_value = sum(ln["value"] or 0.0 for ln in inv["lines"])
            if abs(lines_value - inv["value"]) > VALUE_TOLERANCE:
                inv_issues.append(f"Item values add up to {lines_value:,.2f}, the voucher says {inv['value']:,.2f}.")
        vtype = (inv["voucher_type"] or "").lower()
        if not inv["cancelled"] and not inv["lines"] and "sales" in vtype and "service" not in vtype:
            inv_issues.append("A sales voucher with no item lines.")
        inv["issues"] = inv_issues

        if inv["cancelled"]:
            continue
        by_type[inv["voucher_type"] or "?"] += 1
        by_site[inv["dispatch_from"] or "not given"] += 1
        total_value += inv["value"] or 0.0
        total_gross += inv["gross_total"] or 0.0
        for ln in inv["lines"]:
            line_count += 1
            if ln["kg"] is None:
                lines_without_kg += 1
            else:
                total_kg += ln["kg"]

    grand = parsed["grand_total"]
    if grand and grand.get("value") is not None and abs(grand["value"] - file_value) > VALUE_TOLERANCE:
        issues.append(
            f"The vouchers add up to {file_value:,.2f} but the file's Grand Total is {grand['value']:,.2f} "
            "— some rows may not have been read."
        )
    if grand is None:
        issues.append("No Grand Total row found — the file may be cut short.")

    live = [inv for inv in invoices if not inv["cancelled"]]
    dates = [inv["date"] for inv in live] or [inv["date"] for inv in invoices]
    linked = [inv for inv in live if inv["so_found"]]
    summary = {
        "company": parsed["company"],
        "period": parsed["period"],
        "sheet": parsed["sheet"],
        "invoices": len(live),
        "cancelled": len(invoices) - len(live),
        "by_voucher_type": dict(by_type.most_common()),
        "by_dispatch_site": dict(by_site.most_common()),
        "parties": len({inv["party"] for inv in live}),
        "lines": line_count,
        "total_value": round(total_value, 2),
        "total_gross": round(total_gross, 2),
        "total_kg": round(total_kg, 3),
        "lines_without_kg": lines_without_kg,
        "date_from": min(dates) if dates else None,
        "date_to": max(dates) if dates else None,
        "linked_to_so": len(linked),
        "sos_invoiced": len({inv["so_number"] for inv in linked}),
        "so_not_found": sum(1 for inv in live if inv["quoted_so_number"] and not inv["so_found"]),
        "reference_not_an_so": sum(1 for inv in live
                                   if inv["reference"] and not inv["so_found"] and not inv["quoted_so_number"]),
        "without_reference": sum(1 for inv in live if not inv["reference"]),
        "lines_on_so_line": lines_on_so,
        "lines_not_on_so_line": lines_not_on_so,
        "unmatched_articles": len(unmatched),
        "low_confidence_articles": len(low),
        "invoices_with_issues": sum(1 for inv in invoices if inv["issues"]),
        "duplicate_vouchers": len(duplicates),
        "ledger_columns": parsed["ledger_columns"],
    }
    return {
        "saved": False,
        "save_result": None,
        "summary": summary,
        "issues": issues,
        "unmatched_articles": [{"particulars": n, "lines": c} for n, c in unmatched.most_common()],
        "low_confidence_articles": [
            {"particulars": n, "lines": c, "matched_article": matches[n][0].particulars,
             "match_score": round(matches[n][1], 3)}
            for n, c in low.most_common()
        ],
        "so_numbers_not_found": sorted({inv["quoted_so_number"] for inv in invoices
                                        if inv["quoted_so_number"] and not inv["so_found"]}),
        "invoices": invoices,
    }


_UPSERT_INVOICE = """
INSERT INTO sales_invoice (
    invoice_no, so_number, invoice_date, voucher_type, party, buyer, buyer_address,
    consignee, consignee_address, reference, narration, dispatch_from, cartons,
    replaces_invoice, cancelled, quantity, alt_units, value, gross_total, ledgers,
    source_file, uploaded_by
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19,
          $20::jsonb, $21, $22)
ON CONFLICT (invoice_no) DO UPDATE SET
    so_number = EXCLUDED.so_number, invoice_date = EXCLUDED.invoice_date,
    voucher_type = EXCLUDED.voucher_type, party = EXCLUDED.party, buyer = EXCLUDED.buyer,
    buyer_address = EXCLUDED.buyer_address, consignee = EXCLUDED.consignee,
    consignee_address = EXCLUDED.consignee_address, reference = EXCLUDED.reference,
    narration = EXCLUDED.narration, dispatch_from = EXCLUDED.dispatch_from,
    cartons = EXCLUDED.cartons, replaces_invoice = EXCLUDED.replaces_invoice,
    cancelled = EXCLUDED.cancelled, quantity = EXCLUDED.quantity, alt_units = EXCLUDED.alt_units,
    value = EXCLUDED.value, gross_total = EXCLUDED.gross_total, ledgers = EXCLUDED.ledgers,
    source_file = EXCLUDED.source_file, uploaded_by = EXCLUDED.uploaded_by, updated_at = now()
"""

_INSERT_LINE = """
INSERT INTO sales_invoice_line (
    invoice_no, line_no, particulars, so_line_id, matched_article, match_score,
    quantity, alt_units, kg, kg_source, rate, value
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
"""


async def save_sales_invoices(pool, result: dict[str, Any], *, uploaded_by: str | None,
                              source_file: str | None) -> dict[str, int]:
    """Write a processed upload to sales_invoice / sales_invoice_line in one
    transaction. An invoice number already saved is updated and its lines
    replaced; invoices not in this file are left alone."""
    invoices = result["invoices"]
    if result["summary"]["duplicate_vouchers"]:
        raise ValueError("The file has the same voucher number more than once — fix it in Tally, then upload again.")
    numbers = [inv["voucher_no"] for inv in invoices]
    async with pool.acquire() as conn:
        ready = await conn.fetchval(
            "SELECT to_regclass('public.sales_invoice') IS NOT NULL "
            "AND to_regclass('public.sales_invoice_line') IS NOT NULL")
        if not ready:
            raise InvoiceTablesMissing()
        async with conn.transaction():
            existing = {r["invoice_no"] for r in await conn.fetch(
                "SELECT invoice_no FROM sales_invoice WHERE invoice_no = ANY($1::text[])", numbers)}
            await conn.executemany(_UPSERT_INVOICE, [(
                inv["voucher_no"], inv["so_number"], inv["date"], inv["voucher_type"], inv["party"],
                inv["buyer"], inv["buyer_address"], inv["consignee"], inv["consignee_address"],
                inv["reference"], inv["narration"], inv["dispatch_from"], inv["cartons"],
                inv["replaces_invoice"], inv["cancelled"], inv["quantity"], inv["alt_units"],
                inv["value"], inv["gross_total"], json.dumps(inv["ledgers"]), source_file, uploaded_by,
            ) for inv in invoices])
            await conn.execute("DELETE FROM sales_invoice_line WHERE invoice_no = ANY($1::text[])", numbers)
            line_rows = [(
                inv["voucher_no"], ln["line_no"], ln["particulars"], ln["so_line_id"], ln["matched_article"],
                ln["match_score"], ln["quantity"], ln["alt_units"], ln["kg"], ln["kg_source"],
                ln["rate"], ln["value"],
            ) for inv in invoices for ln in inv["lines"]]
            if line_rows:
                await conn.executemany(_INSERT_LINE, line_rows)
    return {"inserted": len(set(numbers) - existing), "updated": len(set(numbers) & existing),
            "lines": len(line_rows)}
