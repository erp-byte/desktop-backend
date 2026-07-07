"""
Production Module - Entry Counts Report (post-2026-05-22 snapshot).

Mirrors the layout of the 2026-05-22 entry-counts PDF, but restricted to rows
whose creation time is strictly AFTER that report's generation timestamp
(2026-05-22 18:47:40 UTC). Combines v1 and v2 production tables — v2 went
live after the cutoff and now dominates new activity.

Run:  python -m scripts.generate_jc_report_after
Out:  scratch/_job_card_report_after_2026-05-22.pdf
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections import defaultdict
from pathlib import Path

import asyncpg
from dotenv import dotenv_values
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
)


CUTOFF = dt.datetime(2026, 5, 22, 18, 47, 40, tzinfo=dt.timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
OUT_PDF = ROOT / "scratch" / "_job_card_report_after_2026-05-22.pdf"


def fmt_kg(v) -> str:
    if v is None:
        return "0.0"
    return f"{float(v):,.1f}"


def fmt_dt(v) -> str:
    if v is None:
        return "-"
    return v.strftime("%Y-%m-%d %H:%M")


async def fetch_counts(conn: asyncpg.Connection) -> dict:
    """All queries gated on a creation timestamp > CUTOFF."""
    c = CUTOFF
    out: dict = {}

    # --- Header tiles ----------------------------------------------------
    out["plan_headers_v1"] = await conn.fetchval(
        "SELECT COUNT(*) FROM production_plan WHERE created_at > $1", c
    )
    out["plan_headers_v2"] = await conn.fetchval(
        "SELECT COUNT(*) FROM production_plan_v2 WHERE created_at > $1", c
    )
    out["plan_lines_v1"] = await conn.fetchval(
        "SELECT COUNT(*) FROM production_plan_line WHERE created_at > $1", c
    )
    out["plan_lines_v2"] = await conn.fetchval(
        "SELECT COUNT(*) FROM production_plan_line_v2 WHERE created_at > $1", c
    )
    out["plan_steps_v2"] = await conn.fetchval(
        "SELECT COUNT(*) FROM production_plan_step_v2 WHERE created_at > $1", c
    )
    out["plans_latest_v1"] = await conn.fetchval(
        "SELECT MAX(created_at) FROM production_plan WHERE created_at > $1", c
    )
    out["plans_latest_v2"] = await conn.fetchval(
        "SELECT MAX(created_at) FROM production_plan_v2 WHERE created_at > $1", c
    )
    out["plan_lines_latest_v1"] = await conn.fetchval(
        "SELECT MAX(created_at) FROM production_plan_line WHERE created_at > $1", c
    )
    out["plan_lines_latest_v2"] = await conn.fetchval(
        "SELECT MAX(created_at) FROM production_plan_line_v2 WHERE created_at > $1", c
    )

    out["prod_orders"] = await conn.fetchval(
        "SELECT COUNT(*) FROM production_order WHERE created_at > $1", c
    )
    out["prod_orders_latest"] = await conn.fetchval(
        "SELECT MAX(created_at) FROM production_order WHERE created_at > $1", c
    )

    out["jc_total"] = (
        await conn.fetchval("SELECT COUNT(*) FROM job_card    WHERE created_at > $1", c)
        + await conn.fetchval("SELECT COUNT(*) FROM job_card_v2 WHERE created_at > $1", c)
    )
    out["jc_completed"] = (
        await conn.fetchval(
            "SELECT COUNT(*) FROM job_card    WHERE created_at > $1 AND status = 'completed'", c
        )
        + await conn.fetchval(
            "SELECT COUNT(*) FROM job_card_v2 WHERE created_at > $1 AND status IN ('completed','closed')", c
        )
    )

    # --- Production-order status breakdown -------------------------------
    rows = await conn.fetch(
        "SELECT status, COUNT(*) n FROM production_order WHERE created_at > $1 GROUP BY status ORDER BY n DESC",
        c,
    )
    out["po_status"] = [(r["status"], r["n"]) for r in rows]

    # --- Job-card status (v1 + v2 unioned) -------------------------------
    rows = await conn.fetch(
        """
        SELECT status, SUM(n) AS n FROM (
            SELECT status, COUNT(*) n FROM job_card     WHERE created_at > $1 GROUP BY status
            UNION ALL
            SELECT status, COUNT(*) n FROM job_card_v2  WHERE created_at > $1 GROUP BY status
        ) t GROUP BY status ORDER BY n DESC
        """,
        c,
    )
    out["jc_status"] = [(r["status"], int(r["n"])) for r in rows]

    # --- JC detail table counts ------------------------------------------
    detail_specs = [
        ("RM indents",         "job_card_rm_indent",             "job_card_rm_indent_v2",          "created_at", "created_at"),
        ("PM indents",         "job_card_pm_indent",             "job_card_pm_indent_v2",          "created_at", "created_at"),
        ("Process steps",      "job_card_process_step",          None,                              "created_at", None),
        ("Output rows",        "job_card_output",                "job_card_output_v2",             "created_at", "recorded_at"),
        ("Byproducts",         "job_card_byproduct",             "job_card_byproducts_v2",         "created_at", "recorded_at"),
        ("Balance materials",  "job_card_balance_material",      "job_card_balance_material_v2",   "created_at", "created_at"),
        ("Loss recon",         "job_card_loss_reconciliation",   "job_card_loss_reconciliation_v2","created_at", "recorded_at"),
        ("Sign-offs",          "job_card_sign_off",              "job_card_sign_off_v2",           "signed_at",  "signed_at"),
        ("Env readings",       "job_card_environment",           "job_card_environment_v2",        "updated_at", "recorded_at"),
        ("Metal detection",    "job_card_metal_detection",       "job_card_metal_detection_v2",    "updated_at", "recorded_at"),
        ("Weight checks",      "job_card_weight_check",          "job_card_weight_check_v2",       "updated_at", "recorded_at"),
        ("Remarks",            None,                              "job_card_remarks_v2",            None,        "recorded_at"),
        ("Partial dispatches", "job_card_partial_dispatch",      "job_card_partial_dispatch_v2",   "dispatched_at", "dispatched_at"),
    ]
    details = []
    for label, t1, t2, dt1, dt2 in detail_specs:
        total = 0
        latest = None
        for tbl, dtc in [(t1, dt1), (t2, dt2)]:
            if tbl is None:
                continue
            try:
                n = await conn.fetchval(f"SELECT COUNT(*) FROM {tbl} WHERE {dtc} > $1", c)
                mx = await conn.fetchval(f"SELECT MAX({dtc}) FROM {tbl} WHERE {dtc} > $1", c)
            except Exception:
                # Column might be missing on the v1 side — fall back to zero.
                n, mx = 0, None
            total += int(n or 0)
            if mx and (latest is None or mx > latest):
                latest = mx
        details.append((label, total, latest))
    out["details"] = details

    # --- Floor / Article / Stage / RM-indent / Loss ----------------------
    # Build a UNION of v1+v2 JCs created after cutoff, keyed by job_card_id
    # WITHIN its source so we don't collide PKs across schemas.
    out["jc_by_floor"] = await conn.fetch(
        """
        WITH jc AS (
            SELECT floor, COALESCE(batch_size_kg, 0)::numeric AS kg, 'v1' s, job_card_id FROM job_card    WHERE created_at > $1
            UNION ALL
            SELECT floor, COALESCE(planned_qty_kg, 0)::numeric AS kg, 'v2' s, job_card_id FROM job_card_v2 WHERE created_at > $1
        )
        SELECT COALESCE(floor,'(unset)') AS k, COUNT(*) AS cnt, COALESCE(SUM(kg),0) AS kg
        FROM jc GROUP BY 1 ORDER BY cnt DESC, kg DESC
        """,
        c,
    )

    out["jc_by_article"] = await conn.fetch(
        """
        WITH jc AS (
            SELECT fg_sku_name, COALESCE(batch_size_kg, 0)::numeric AS kg FROM job_card    WHERE created_at > $1
            UNION ALL
            SELECT fg_sku_name, COALESCE(planned_qty_kg, 0)::numeric AS kg FROM job_card_v2 WHERE created_at > $1
        )
        SELECT COALESCE(fg_sku_name,'(unset)') AS k, COUNT(*) AS cnt, COALESCE(SUM(kg),0) AS kg
        FROM jc GROUP BY 1 ORDER BY cnt DESC, kg DESC LIMIT 12
        """,
        c,
    )

    out["jc_by_stage"] = await conn.fetch(
        """
        WITH jc AS (
            SELECT stage, COALESCE(batch_size_kg, 0)::numeric AS kg FROM job_card    WHERE created_at > $1
            UNION ALL
            SELECT stage, COALESCE(planned_qty_kg, 0)::numeric AS kg FROM job_card_v2 WHERE created_at > $1
        )
        SELECT COALESCE(stage,'(unset)') AS k, COUNT(*) AS cnt, COALESCE(SUM(kg),0) AS kg
        FROM jc GROUP BY 1 ORDER BY cnt DESC, kg DESC
        """,
        c,
    )

    # RM-indent status (issued_qty is the "kg issued" column on both v1 and v2)
    out["rm_status"] = await conn.fetch(
        """
        WITH rm AS (
            SELECT r.status AS status, COALESCE(r.issued_qty,0)::numeric AS kg
              FROM job_card_rm_indent r
              JOIN job_card jc USING (job_card_id)
             WHERE jc.created_at > $1
            UNION ALL
            SELECT r.status AS status, COALESCE(r.issued_qty,0)::numeric AS kg
              FROM job_card_rm_indent_v2 r
              JOIN job_card_v2 jc USING (job_card_id)
             WHERE jc.created_at > $1
        )
        SELECT status AS k, COUNT(*) AS cnt, COALESCE(SUM(kg),0) AS kg
        FROM rm GROUP BY status ORDER BY cnt DESC
        """,
        c,
    )

    out["loss_by_cat"] = await conn.fetch(
        """
        WITH lr AS (
            SELECT l.loss_category AS loss_category,
                   COALESCE(l.actual_loss_kg,0)::numeric AS kg
              FROM job_card_loss_reconciliation l
              JOIN job_card jc USING (job_card_id)
             WHERE jc.created_at > $1 AND l.deleted_at IS NULL
            UNION ALL
            SELECT l.loss_category AS loss_category,
                   COALESCE(l.actual_loss_qty,0)::numeric AS kg
              FROM job_card_loss_reconciliation_v2 l
              JOIN job_card_v2 jc USING (job_card_id)
             WHERE jc.created_at > $1 AND l.deleted_at IS NULL
        )
        SELECT loss_category AS k, COUNT(*) AS cnt, COALESCE(SUM(kg),0) AS kg
        FROM lr GROUP BY loss_category ORDER BY cnt DESC
        """,
        c,
    )

    return out


async def fetch_pivot_rows(conn: asyncpg.Connection):
    """Return rows for the Floor × Article × Stage-of-closure pivot."""
    c = CUTOFF
    rows = await conn.fetch(
        """
        WITH jc AS (
            SELECT job_card_id, floor, fg_sku_name, stage, status,
                   COALESCE(batch_size_kg,0)::numeric kg,
                   'v1' src
              FROM job_card
             WHERE created_at > $1
            UNION ALL
            SELECT job_card_id, floor, fg_sku_name, stage, status,
                   COALESCE(planned_qty_kg,0)::numeric kg,
                   'v2' src
              FROM job_card_v2
             WHERE created_at > $1
        ),
        loss AS (
            SELECT src, job_card_id, SUM(loss_qty) loss_kg FROM (
                SELECT 'v1' src, job_card_id, COALESCE(actual_loss_kg,0)::numeric loss_qty
                  FROM job_card_loss_reconciliation
                 WHERE loss_category = 'total_loss' AND deleted_at IS NULL
                UNION ALL
                SELECT 'v2' src, job_card_id, COALESCE(actual_loss_qty,0)::numeric loss_qty
                  FROM job_card_loss_reconciliation_v2
                 WHERE loss_category = 'total_loss' AND deleted_at IS NULL
            ) t GROUP BY 1, 2
        )
        SELECT jc.floor, jc.fg_sku_name, jc.stage, jc.status, jc.kg,
               COALESCE(loss.loss_kg, 0) AS loss_kg
          FROM jc
          LEFT JOIN loss ON loss.src = jc.src AND loss.job_card_id = jc.job_card_id
        """,
        c,
    )
    return rows


def build_kpi_tiles(counts: dict):
    plan_total = (counts["plan_headers_v1"] + counts["plan_headers_v2"]
                  + counts["plan_lines_v1"]  + counts["plan_lines_v2"])
    cells = [
        ["Plans + plan lines", "Production orders", "Job cards (all)", "Job cards complete"],
        [str(plan_total), str(counts["prod_orders"]), str(counts["jc_total"]), str(counts["jc_completed"])],
    ]
    tbl = Table(cells, colWidths=[65*mm]*4, rowHeights=[8*mm, 20*mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3B6BB0")),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, 0), 9),
        ("ALIGN",      (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("FONTSIZE",   (0, 1), (-1, 1), 30),
        ("FONTNAME",   (0, 1), (-1, 1), "Helvetica-Bold"),
        ("BOX",        (0, 0), (-1, -1), 0.5, colors.grey),
        ("INNERGRID",  (0, 0), (-1, -1), 0.25, colors.grey),
    ]))
    return tbl


def section_header(text: str, styles) -> Paragraph:
    return Paragraph(f"<b>{text}</b>", styles["Heading4"])


def small_table(header_row, body_rows, col_widths):
    data = [header_row] + body_rows
    tbl = Table(data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E6ECF5")),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, -1), 7.5),
        ("ALIGN",      (1, 1), (-1, -1), "RIGHT"),
        ("ALIGN",      (0, 0), (0, -1), "LEFT"),
        ("BOX",        (0, 0), (-1, -1), 0.4, colors.grey),
        ("INNERGRID",  (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ("TOPPADDING",    (0, 0), (-1, -1), 1.5),
    ]))
    return tbl


def build_planning_table(counts):
    body = [
        ["Plan headers",       str(counts["plan_headers_v1"] + counts["plan_headers_v2"]),
            fmt_dt(max([d for d in [counts["plans_latest_v1"], counts["plans_latest_v2"]] if d], default=None))],
        ["Plan lines (SKU x period)", str(counts["plan_lines_v1"] + counts["plan_lines_v2"]),
            fmt_dt(max([d for d in [counts["plan_lines_latest_v1"], counts["plan_lines_latest_v2"]] if d], default=None))],
        ["Plan steps (v2)",    str(counts["plan_steps_v2"]),
            fmt_dt(counts["plans_latest_v2"])],
    ]
    return small_table(["Table", "Rows", "Latest"], body, [33*mm, 12*mm, 22*mm])


def build_po_tables(counts):
    pos = [
        ["Production orders", str(counts["prod_orders"]), fmt_dt(counts["prod_orders_latest"])],
    ]
    tbl1 = small_table(["Table", "Rows", "Latest"], pos, [33*mm, 12*mm, 22*mm])

    total = sum(n for _, n in counts["po_status"]) or 1
    body = [[s, str(n), f"{n/total*100:.1f}%"] for s, n in counts["po_status"]]
    if not body:
        body = [["(no rows)", "0", "0.0%"]]
    tbl2 = small_table(["Status", "Rows", "% of total"], body, [33*mm, 12*mm, 22*mm])
    return tbl1, tbl2


def build_jc_status_table(counts):
    total = sum(n for _, n in counts["jc_status"]) or 1
    body = [[s, str(n), f"{n/total*100:.1f}%"] for s, n in counts["jc_status"]]
    if not body:
        body = [["(no rows)", "0", "0.0%"]]
    return small_table(["Status", "Rows", "% of total"], body, [33*mm, 12*mm, 22*mm])


def build_detail_table(counts):
    body = [[lbl, str(n), fmt_dt(latest)] for (lbl, n, latest) in counts["details"]]
    return small_table(["Table", "Rows", "Latest"], body, [33*mm, 12*mm, 22*mm])


def build_keyed_table(rows, header, col_widths, limit=None):
    body = []
    rows_list = list(rows)
    if limit:
        rows_list = rows_list[:limit]
    for r in rows_list:
        body.append([str(r["k"]), str(r["cnt"]), fmt_kg(r["kg"])])
    if not body:
        body = [["(no rows)", "0", "0.0"]]
    return small_table(header, body, col_widths)


def build_rm_status(rows):
    body = [[r["k"], str(r["cnt"]), fmt_kg(r["kg"])] for r in rows]
    if not body:
        body = [["(no rows)", "0", "0.0"]]
    return small_table(["Key", "Count", "Issued kg"], body, [22*mm, 12*mm, 18*mm])


def build_loss_table(rows):
    body = [[r["k"], str(r["cnt"]), fmt_kg(r["kg"])] for r in rows]
    if not body:
        body = [["(no rows)", "0", "0.0"]]
    return small_table(["Key", "Count", "Loss kg"], body, [22*mm, 12*mm, 18*mm])


def build_pivot(pivot_rows):
    """Floor x Article: closed JC count by stage, plus summary columns."""
    closed_set = {"completed", "closed"}
    inprog_set = {"in_progress", "material_received", "assigned"}

    # Discover the union of stages that have at least one closed JC.
    stages_with_closed = sorted({
        r["stage"] for r in pivot_rows
        if (r["status"] or "").lower() in closed_set and r["stage"]
    })

    keys = {}
    for r in pivot_rows:
        k = ((r["floor"] or "(unset)"), (r["fg_sku_name"] or "(unset)"))
        rec = keys.setdefault(k, {
            "stage_closed": defaultdict(int),
            "closed": 0, "inprog": 0, "pending": 0,
            "total": 0, "kg": 0.0, "loss_kg": 0.0,
        })
        st = (r["status"] or "").lower()
        if st in closed_set:
            rec["closed"] += 1
            if r["stage"]:
                rec["stage_closed"][r["stage"]] += 1
        elif st in inprog_set:
            rec["inprog"] += 1
        else:
            rec["pending"] += 1
        rec["total"] += 1
        rec["kg"]  += float(r["kg"] or 0)
        rec["loss_kg"] += float(r["loss_kg"] or 0)

    # Hide rows with zero closed AND zero in-progress (matches the original).
    visible = {k: v for k, v in keys.items() if (v["closed"] + v["inprog"]) > 0}
    hidden_count = len(keys) - len(visible)

    # Sort: floor asc, then fg_sku asc.
    sorted_keys = sorted(visible.items(), key=lambda kv: (kv[0][0], kv[0][1]))

    header = ["Floor", "FG SKU"] + stages_with_closed + ["Closed", "InProg", "Pending", "TotalJC", "Batch kg", "Loss kg"]

    body_rows = []
    grand = {s: 0 for s in stages_with_closed}
    g_closed = g_inprog = g_pending = g_total = 0
    g_kg = g_loss = 0.0
    for (floor, sku), rec in sorted_keys:
        row = [floor[:18], sku[:42]]
        for s in stages_with_closed:
            v = rec["stage_closed"].get(s, 0)
            row.append(str(v) if v else "-")
            grand[s] += v
        row += [
            str(rec["closed"]) if rec["closed"] else "-",
            str(rec["inprog"]) if rec["inprog"] else "-",
            str(rec["pending"]) if rec["pending"] else "-",
            str(rec["total"]),
            fmt_kg(rec["kg"]),
            fmt_kg(rec["loss_kg"]),
        ]
        body_rows.append(row)
        g_closed  += rec["closed"]
        g_inprog  += rec["inprog"]
        g_pending += rec["pending"]
        g_total   += rec["total"]
        g_kg      += rec["kg"]
        g_loss    += rec["loss_kg"]

    grand_row = ["GRAND TOTAL", ""]
    for s in stages_with_closed:
        grand_row.append(str(grand[s]) if grand[s] else "-")
    grand_row += [str(g_closed), str(g_inprog), str(g_pending), str(g_total),
                  fmt_kg(g_kg), fmt_kg(g_loss)]

    data = [header] + body_rows + [grand_row]
    n_stage = len(stages_with_closed)
    col_widths = [28*mm, 60*mm] + [22*mm] * n_stage + [14*mm, 14*mm, 14*mm, 16*mm, 20*mm, 18*mm]
    tbl = Table(data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3B6BB0")),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, -1), 7),
        ("ALIGN",      (2, 1), (-1, -1), "RIGHT"),
        ("ALIGN",      (0, 0), (1, -1), "LEFT"),
        ("BOX",        (0, 0), (-1, -1), 0.4, colors.grey),
        ("INNERGRID",  (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("FONTNAME",   (0, -1), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F0F0F0")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.2),
        ("TOPPADDING",    (0, 0), (-1, -1), 1.2),
    ]))
    return tbl, hidden_count


async def main():
    env = dotenv_values(ROOT / ".env")
    conn = await asyncpg.connect(env["DATABASE_URL"])
    try:
        counts = await fetch_counts(conn)
        pivot_rows = await fetch_pivot_rows(conn)
    finally:
        await conn.close()

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Tiny", fontSize=7, leading=8))
    styles.add(ParagraphStyle(name="SubHead", fontSize=8, leading=10, textColor=colors.HexColor("#444444")))

    OUT_PDF.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(OUT_PDF),
        pagesize=landscape(A4),
        leftMargin=12*mm, rightMargin=12*mm,
        topMargin=12*mm, bottomMargin=12*mm,
        title="Production Module - Entry Counts Report (post-2026-05-22)",
    )

    story = []
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    story.append(Paragraph(
        "<b>Production Module - Entry Counts Report</b>", styles["Title"],
    ))
    story.append(Paragraph(
        f"Window: rows created AFTER {CUTOFF.strftime('%Y-%m-%d %H:%M')} UTC "
        f"(date of the previous report)<br/>"
        f"Generated {generated} UTC / Database: warehouse_db / Includes v1 + v2 schemas",
        styles["SubHead"],
    ))
    story.append(Spacer(1, 4*mm))
    story.append(build_kpi_tiles(counts))
    story.append(Spacer(1, 5*mm))

    # Row 1: 4 small tables side-by-side
    planning_tbl = build_planning_table(counts)
    po_tbl, po_status_tbl = build_po_tables(counts)
    jc_status_tbl = build_jc_status_table(counts)
    detail_tbl = build_detail_table(counts)

    row1 = Table(
        [
            [
                Paragraph("<b>Planning</b>", styles["SubHead"]),
                Paragraph("<b>Production orders</b>", styles["SubHead"]),
                Paragraph("<b>Job-card status breakdown</b>", styles["SubHead"]),
                Paragraph("<b>Job-card detail tables</b>", styles["SubHead"]),
            ],
            [planning_tbl, [po_tbl, Spacer(1, 1.5*mm), po_status_tbl], jc_status_tbl, detail_tbl],
        ],
        colWidths=[70*mm, 70*mm, 65*mm, 70*mm],
    )
    row1.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    story.append(row1)
    story.append(Spacer(1, 6*mm))

    story.append(Paragraph("<b>Operational breakdowns</b>", styles["Heading3"]))
    story.append(Spacer(1, 2*mm))

    floor_tbl   = build_keyed_table(counts["jc_by_floor"],  ["Key", "Count", "kg"],         [22*mm, 12*mm, 18*mm])
    article_tbl = build_keyed_table(counts["jc_by_article"],["Key", "Count", "kg"],         [38*mm, 12*mm, 18*mm])
    rm_tbl      = build_rm_status(counts["rm_status"])
    stage_tbl   = build_keyed_table(counts["jc_by_stage"],  ["Key", "Count", "kg"],         [28*mm, 12*mm, 18*mm])
    loss_tbl    = build_loss_table(counts["loss_by_cat"])

    row2 = Table(
        [[
            [Paragraph("<b>Floor-wise (JC count + batch kg)</b>", styles["SubHead"]), Spacer(1, 1*mm), floor_tbl],
            [Paragraph("<b>Article-wise (top FG SKUs)</b>",       styles["SubHead"]), Spacer(1, 1*mm), article_tbl],
            [Paragraph("<b>Entry inputs (RM indent status)</b>",  styles["SubHead"]), Spacer(1, 1*mm), rm_tbl],
            [Paragraph("<b>Stage-wise (JC count)</b>",            styles["SubHead"]), Spacer(1, 1*mm), stage_tbl],
            [Paragraph("<b>Loss calculation (by category)</b>",   styles["SubHead"]), Spacer(1, 1*mm), loss_tbl],
        ]],
        colWidths=[52*mm, 70*mm, 38*mm, 60*mm, 42*mm],
    )
    row2.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(row2)
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph(
        "<i>Counts are point-in-time snapshots from warehouse_db. "
        "Latest = MAX(created_at|updated_at|recorded_at) where present.</i>",
        styles["Tiny"],
    ))

    # ---- Page 2: pivot ---------------------------------------------------
    story.append(PageBreak())
    story.append(Paragraph("<b>Production Module - Entry Counts Report</b>", styles["Title"]))
    story.append(Paragraph(
        f"Window: rows created AFTER {CUTOFF.strftime('%Y-%m-%d %H:%M')} UTC<br/>"
        f"Generated {generated} UTC / Database: warehouse_db",
        styles["SubHead"],
    ))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph("<b>Floor x Article x Stage-of-closure Pivot</b>", styles["Heading3"]))
    story.append(Paragraph(
        "Stage columns count CLOSED job cards (status in completed/closed). "
        "Summary columns split closed / in-progress / pending.",
        styles["SubHead"],
    ))
    story.append(Spacer(1, 2*mm))

    pivot_tbl, hidden = build_pivot(pivot_rows)
    story.append(pivot_tbl)
    story.append(Spacer(1, 2*mm))
    if hidden:
        story.append(Paragraph(
            f"<i>Hidden {hidden} (floor, FG SKU) combinations with zero closed and zero in-progress JCs.</i>",
            styles["Tiny"],
        ))

    doc.build(story)
    print(f"Wrote {OUT_PDF}")


if __name__ == "__main__":
    asyncio.run(main())
