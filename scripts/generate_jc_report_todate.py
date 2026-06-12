"""
Production Module - Entry Counts Report (cumulative, till date).

Mirrors the layout of the 2026-05-22 entry-counts PDF, but counts ALL rows
through the moment of generation (no cutoff). Combines v1 and v2 production
tables — v2 went live after 2026-05-22 and now dominates new activity, so the
cumulative picture spans both schemas.

Run:  python -m scripts.generate_jc_report_todate
Out:  scratch/_job_card_report_todate.pdf
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
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.units import mm
from xml.sax.saxutils import escape
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
    KeepTogether,
)


# Far-past sentinel: every `created_at > CUTOFF` predicate matches all rows,
# so the same queries that powered the post-cutoff report now run cumulatively.
CUTOFF = dt.datetime(2000, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
OUT_PDF = ROOT / "scratch" / "_job_card_report_todate.pdf"

# Usable content width on landscape A4 (297mm) minus 12mm margins each side.
USABLE_W = 273 * mm

# Cell paragraph styles — wrapping text prevents long values (SKU names,
# datetimes) from overflowing their columns and overlapping neighbours.
_CELL   = ParagraphStyle("cell",   fontName="Helvetica",      fontSize=7, leading=8)
_CELL_R = ParagraphStyle("cellR",  fontName="Helvetica",      fontSize=7, leading=8, alignment=TA_RIGHT)
_HEAD   = ParagraphStyle("head",   fontName="Helvetica-Bold", fontSize=7, leading=8)
_HEAD_R = ParagraphStyle("headR",  fontName="Helvetica-Bold", fontSize=7, leading=8, alignment=TA_RIGHT)
# White header text for tables with a dark-blue header background.
_HEADW   = ParagraphStyle("headW",  fontName="Helvetica-Bold", fontSize=7, leading=8, textColor=colors.white)
_HEADW_R = ParagraphStyle("headWR", fontName="Helvetica-Bold", fontSize=7, leading=8, textColor=colors.white, alignment=TA_RIGHT)


def _cells(row, is_header=False, left_cols=(0,), bold=False):
    """Wrap each value in a Paragraph; columns in left_cols are left-aligned,
    the rest right-aligned. Wrapping lets text flow to multiple lines instead
    of spilling past the column boundary. bold=True renders a totals row."""
    out = []
    for i, val in enumerate(row):
        left = i in left_cols
        if is_header or bold:
            style = _HEAD if left else _HEAD_R
        else:
            style = _CELL if left else _CELL_R
        out.append(Paragraph(escape(str(val)), style))
    return out


def build_grid_table(header, body_rows, col_widths, left_cols=(0,), total_row=False):
    """Generic blue-header grid table with wrapped cells and an optional
    bold/shaded last (totals) row."""
    header_cells = [
        Paragraph(escape(str(v)), _HEADW if i in left_cols else _HEADW_R)
        for i, v in enumerate(header)
    ]
    data = [header_cells]
    last = len(body_rows) - 1
    for j, r in enumerate(body_rows):
        data.append(_cells(r, left_cols=left_cols, bold=total_row and j == last))
    tbl = Table(data, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND",   (0, 0), (-1, 0), colors.HexColor("#3B6BB0")),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("BOX",          (0, 0), (-1, -1), 0.4, colors.grey),
        ("INNERGRID",    (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("LEFTPADDING",  (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 1.5),
        ("TOPPADDING",   (0, 0), (-1, -1), 1.5),
    ]
    if total_row:
        style.append(("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F0F0F0")))
    tbl.setStyle(TableStyle(style))
    return tbl


def fmt_kg(v) -> str:
    if v is None:
        return "0.0"
    return f"{float(v):,.1f}"


def fmt_dt(v) -> str:
    if v is None:
        return "-"
    return v.strftime("%Y-%m-%d %H:%M")


async def fetch_counts(conn: asyncpg.Connection) -> dict:
    """All queries gated on a creation timestamp > CUTOFF (far past == all rows)."""
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
    # Terminal completions (status reached completed/closed).
    out["jc_terminal"] = (
        await conn.fetchval(
            "SELECT COUNT(*) FROM job_card    WHERE created_at > $1 AND status = 'completed'", c
        )
        + await conn.fetchval(
            "SELECT COUNT(*) FROM job_card_v2 WHERE created_at > $1 AND status IN ('completed','closed')", c
        )
    )
    # "Filled but stuck": production output was recorded but the JC never
    # reached a terminal status — typically blocked by the completion gate
    # (R9 unbalanced accounting / open batch). These are effectively done.
    out["jc_stuck"] = (
        await conn.fetchval(
            """SELECT COUNT(*) FROM job_card jc
               WHERE created_at > $1 AND status <> 'completed'
                 AND EXISTS (SELECT 1 FROM job_card_output o WHERE o.job_card_id = jc.job_card_id)""",
            c,
        )
        + await conn.fetchval(
            """SELECT COUNT(*) FROM job_card_v2 jc
               WHERE created_at > $1 AND status NOT IN ('completed','closed')
                 AND EXISTS (SELECT 1 FROM job_card_output_v2 o WHERE o.job_card_id = jc.job_card_id)""",
            c,
        )
    )
    # Effective complete = terminal + filled-but-stuck.
    out["jc_completed"] = out["jc_terminal"] + out["jc_stuck"]

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
            SELECT j.job_card_id, j.floor, j.fg_sku_name, j.stage, j.status,
                   COALESCE(j.batch_size_kg,0)::numeric kg,
                   'v1' src,
                   EXISTS (SELECT 1 FROM job_card_output o WHERE o.job_card_id = j.job_card_id) AS has_output
              FROM job_card j
             WHERE j.created_at > $1
            UNION ALL
            SELECT j.job_card_id, j.floor, j.fg_sku_name, j.stage, j.status,
                   COALESCE(j.planned_qty_kg,0)::numeric kg,
                   'v2' src,
                   EXISTS (SELECT 1 FROM job_card_output_v2 o WHERE o.job_card_id = j.job_card_id) AS has_output
              FROM job_card_v2 j
             WHERE j.created_at > $1
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
        SELECT jc.floor, jc.fg_sku_name, jc.stage, jc.status, jc.has_output, jc.kg,
               COALESCE(loss.loss_kg, 0) AS loss_kg
          FROM jc
          LEFT JOIN loss ON loss.src = jc.src AND loss.job_card_id = jc.job_card_id
        """,
        c,
    )
    return rows


async def fetch_weekly(conn: asyncpg.Connection):
    """Bucket entries by ISO week (Monday, UTC) of their creation timestamp.

    Returns (summary_rows, status_list, status_map) where summary_rows is one
    dict per week with job-card / order / plan / indent counts plus batch and
    loss kg; status_map[wk][status] = job-card count.
    """
    # --- Per-week job-card summary (v1 + v2), with kg and total_loss kg ----
    jc = await conn.fetch(
        """
        WITH jc AS (
            SELECT date_trunc('week', j.created_at AT TIME ZONE 'UTC') AS wk, j.status,
                   COALESCE(j.batch_size_kg,0)::numeric AS kg, j.job_card_id, 'v1' src,
                   EXISTS (SELECT 1 FROM job_card_output o WHERE o.job_card_id = j.job_card_id) AS has_output
              FROM job_card j
            UNION ALL
            SELECT date_trunc('week', j.created_at AT TIME ZONE 'UTC') AS wk, j.status,
                   COALESCE(j.planned_qty_kg,0)::numeric AS kg, j.job_card_id, 'v2' src,
                   EXISTS (SELECT 1 FROM job_card_output_v2 o WHERE o.job_card_id = j.job_card_id) AS has_output
              FROM job_card_v2 j
        ),
        loss AS (
            SELECT 'v1' src, job_card_id, COALESCE(SUM(actual_loss_kg),0)::numeric loss_kg
              FROM job_card_loss_reconciliation
             WHERE loss_category='total_loss' AND deleted_at IS NULL GROUP BY job_card_id
            UNION ALL
            SELECT 'v2' src, job_card_id, COALESCE(SUM(actual_loss_qty),0)::numeric loss_kg
              FROM job_card_loss_reconciliation_v2
             WHERE loss_category='total_loss' AND deleted_at IS NULL GROUP BY job_card_id
        )
        SELECT jc.wk,
               COUNT(*) AS jc_count,
               COUNT(*) FILTER (WHERE jc.status IN ('completed','closed')) AS jc_compl,
               COUNT(*) FILTER (WHERE jc.status NOT IN ('completed','closed') AND jc.has_output) AS jc_stuck,
               COALESCE(SUM(jc.kg),0) AS kg,
               COALESCE(SUM(l.loss_kg),0) AS loss_kg
          FROM jc LEFT JOIN loss l ON l.src=jc.src AND l.job_card_id=jc.job_card_id
         GROUP BY jc.wk
        """,
    )

    async def weekly_count(union_sql: str):
        rows = await conn.fetch(union_sql)
        return {r["wk"]: int(r["n"]) for r in rows}

    po = await weekly_count(
        "SELECT date_trunc('week', created_at AT TIME ZONE 'UTC') wk, COUNT(*) n "
        "FROM production_order GROUP BY 1"
    )
    ph = await weekly_count(
        """
        SELECT wk, SUM(n) n FROM (
            SELECT date_trunc('week', created_at AT TIME ZONE 'UTC') wk, COUNT(*) n FROM production_plan    GROUP BY 1
            UNION ALL
            SELECT date_trunc('week', created_at AT TIME ZONE 'UTC') wk, COUNT(*) n FROM production_plan_v2 GROUP BY 1
        ) t GROUP BY wk
        """
    )
    pl = await weekly_count(
        """
        SELECT wk, SUM(n) n FROM (
            SELECT date_trunc('week', created_at AT TIME ZONE 'UTC') wk, COUNT(*) n FROM production_plan_line    GROUP BY 1
            UNION ALL
            SELECT date_trunc('week', created_at AT TIME ZONE 'UTC') wk, COUNT(*) n FROM production_plan_line_v2 GROUP BY 1
        ) t GROUP BY wk
        """
    )
    rm = await weekly_count(
        """
        SELECT wk, SUM(n) n FROM (
            SELECT date_trunc('week', created_at AT TIME ZONE 'UTC') wk, COUNT(*) n FROM job_card_rm_indent    GROUP BY 1
            UNION ALL
            SELECT date_trunc('week', created_at AT TIME ZONE 'UTC') wk, COUNT(*) n FROM job_card_rm_indent_v2 GROUP BY 1
        ) t GROUP BY wk
        """
    )

    jc_map = {r["wk"]: r for r in jc}
    # Drive rows off the union of every source's weeks, so a plan or order
    # created in a week with no job cards still gets its own row (and counts
    # toward the totals). Drop NULL weeks (rows with no creation timestamp),
    # which the cumulative snapshot also excludes.
    all_weeks = sorted(
        w for w in set(jc_map) | set(po) | set(ph) | set(pl) | set(rm)
        if w is not None
    )
    summary = []
    for wk in all_weeks:
        r = jc_map.get(wk)
        compl = int(r["jc_compl"]) if r else 0
        stuck = int(r["jc_stuck"]) if r else 0
        summary.append({
            "wk": wk,
            "jc_count": int(r["jc_count"]) if r else 0,
            "jc_compl": compl,
            "jc_stuck": stuck,
            "jc_done": compl + stuck,
            "kg": float(r["kg"] or 0) if r else 0.0,
            "loss_kg": float(r["loss_kg"] or 0) if r else 0.0,
            "po": po.get(wk, 0),
            "ph": ph.get(wk, 0),
            "pl": pl.get(wk, 0),
            "rm": rm.get(wk, 0),
        })

    # --- Per-week job-card status pivot ----------------------------------
    st = await conn.fetch(
        """
        WITH jc AS (
            SELECT date_trunc('week', created_at AT TIME ZONE 'UTC') wk, status FROM job_card
            UNION ALL
            SELECT date_trunc('week', created_at AT TIME ZONE 'UTC') wk, status FROM job_card_v2
        )
        SELECT wk, status, COUNT(*) n FROM jc GROUP BY wk, status
        """,
    )
    status_map: dict = defaultdict(lambda: defaultdict(int))
    status_totals: dict = defaultdict(int)
    for r in st:
        s = r["status"] or "(unset)"
        status_map[r["wk"]][s] += int(r["n"])
        status_totals[s] += int(r["n"])
    status_list = [s for s, _ in sorted(status_totals.items(), key=lambda kv: -kv[1])]

    return summary, status_list, status_map


def build_weekly_summary(summary):
    header = ["Week (Mon)", "JCs", "Compl", "Stuck*", "Done", "Prod ord",
              "Plan hdr", "Plan ln", "RM ind", "Batch kg", "Loss kg"]
    widths = [26*mm, 13*mm, 14*mm, 14*mm, 13*mm, 16*mm, 15*mm, 14*mm, 14*mm, 22*mm, 20*mm]
    body = []
    tot = {"jc_count": 0, "jc_compl": 0, "jc_stuck": 0, "jc_done": 0,
           "po": 0, "ph": 0, "pl": 0, "rm": 0, "kg": 0.0, "loss_kg": 0.0}
    for r in summary:
        body.append([
            r["wk"].strftime("%Y-%m-%d"),
            r["jc_count"], r["jc_compl"], r["jc_stuck"], r["jc_done"],
            r["po"], r["ph"], r["pl"], r["rm"],
            fmt_kg(r["kg"]), fmt_kg(r["loss_kg"]),
        ])
        for k in tot:
            tot[k] += r[k]
    if not summary:
        return build_grid_table(header, [["(no rows)"] + ["0"] * 10], widths)
    body.append([
        "TOTAL", tot["jc_count"], tot["jc_compl"], tot["jc_stuck"], tot["jc_done"],
        tot["po"], tot["ph"], tot["pl"], tot["rm"],
        fmt_kg(tot["kg"]), fmt_kg(tot["loss_kg"]),
    ])
    return build_grid_table(header, body, widths, left_cols=(0,), total_row=True)


def build_weekly_status(summary, status_list, status_map):
    weeks = [r["wk"] for r in summary]
    header = ["Week (Mon)"] + status_list + ["Total"]
    n = len(status_list)
    week_w, total_w = 30*mm, 18*mm
    stage_w = max(14*mm, min(26*mm, (USABLE_W - week_w - total_w) / n)) if n else 0
    widths = [week_w] + [stage_w] * n + [total_w]

    body = []
    col_tot = {s: 0 for s in status_list}
    grand = 0
    for wk in weeks:
        row = [wk.strftime("%Y-%m-%d")]
        rowtot = 0
        for s in status_list:
            v = status_map.get(wk, {}).get(s, 0)
            row.append(v if v else "-")
            col_tot[s] += v
            rowtot += v
        row.append(rowtot)
        grand += rowtot
        body.append(row)
    body.append(["TOTAL"] + [col_tot[s] for s in status_list] + [grand])
    if not weeks:
        return build_grid_table(header, [["(no rows)"] + ["-"] * (n + 1)], widths)
    return build_grid_table(header, body, widths, left_cols=(0,), total_row=True)


def build_kpi_tiles(counts: dict):
    plan_total = (counts["plan_headers_v1"] + counts["plan_headers_v2"]
                  + counts["plan_lines_v1"]  + counts["plan_lines_v2"])
    cells = [
        ["Plans + plan lines", "Production orders", "Job cards (all)", "JC complete (effective)*"],
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
    data = [_cells(header_row, is_header=True)] + [_cells(r) for r in body_rows]
    tbl = Table(data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E6ECF5")),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("BOX",        (0, 0), (-1, -1), 0.4, colors.grey),
        ("INNERGRID",  (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("LEFTPADDING",   (0, 0), (-1, -1), 2),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 2),
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
    return small_table(["Table", "Rows", "Latest"], body, [30*mm, 11*mm, 19*mm])


def build_po_tables(counts):
    pos = [
        ["Production orders", str(counts["prod_orders"]), fmt_dt(counts["prod_orders_latest"])],
    ]
    tbl1 = small_table(["Table", "Rows", "Latest"], pos, [30*mm, 11*mm, 19*mm])

    total = sum(n for _, n in counts["po_status"]) or 1
    body = [[s, str(n), f"{n/total*100:.1f}%"] for s, n in counts["po_status"]]
    if not body:
        body = [["(no rows)", "0", "0.0%"]]
    tbl2 = small_table(["Status", "Rows", "% of total"], body, [30*mm, 11*mm, 19*mm])
    return tbl1, tbl2


def build_jc_status_table(counts):
    total = sum(n for _, n in counts["jc_status"]) or 1
    body = [[s, str(n), f"{n/total*100:.1f}%"] for s, n in counts["jc_status"]]
    if not body:
        body = [["(no rows)", "0", "0.0%"]]
    return small_table(["Status", "Rows", "% of total"], body, [30*mm, 11*mm, 19*mm])


def build_completion_view(counts):
    """Effective-completion view: terminal + filled-but-stuck = effective."""
    total = counts["jc_total"] or 1
    term = counts["jc_terminal"]
    stuck = counts["jc_stuck"]
    eff = counts["jc_completed"]
    body = [
        ["Completed / closed", str(term),  f"{term/total*100:.1f}%"],
        ["Filled but stuck*",  str(stuck), f"{stuck/total*100:.1f}%"],
        ["Effective complete", str(eff),   f"{eff/total*100:.1f}%"],
    ]
    return small_table(["Completion view", "JCs", "% all"], body, [30*mm, 11*mm, 19*mm])


def build_detail_table(counts):
    body = [[lbl, str(n), fmt_dt(latest)] for (lbl, n, latest) in counts["details"]]
    return small_table(["Table", "Rows", "Latest"], body, [30*mm, 11*mm, 19*mm])


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
    return small_table(["Key", "Count", "Issued kg"], body, [21*mm, 10*mm, 16*mm])


def build_loss_table(rows):
    body = [[r["k"], str(r["cnt"]), fmt_kg(r["kg"])] for r in rows]
    if not body:
        body = [["(no rows)", "0", "0.0"]]
    return small_table(["Key", "Count", "Loss kg"], body, [21*mm, 10*mm, 16*mm])


def build_pivot(pivot_rows):
    """Floor x Article: done JC count by stage, plus summary columns.

    "Done" = effectively complete: a terminal status (completed/closed) OR a
    JC that has recorded production output but is stuck short of completion
    (the completion-gate error). Stuck JCs no longer count as in-progress.
    """
    closed_set = {"completed", "closed"}
    inprog_set = {"in_progress", "material_received", "assigned"}

    def is_done(r):
        return (r["status"] or "").lower() in closed_set or r["has_output"]

    # Discover the union of stages that have at least one done JC.
    stages_with_closed = sorted({
        r["stage"] for r in pivot_rows if is_done(r) and r["stage"]
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
        if is_done(r):
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

    header = ["Floor", "FG SKU"] + stages_with_closed + ["Done", "InProg", "Pending", "TotalJC", "Batch kg", "Loss kg"]

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

    n_stage = len(stages_with_closed)

    # Fixed columns: floor, FG SKU, and the 6 summary columns. Stage columns
    # share whatever width is left so the table always fits the page, however
    # many stages have closed JCs in this snapshot.
    floor_w = 24 * mm
    sku_w = 52 * mm
    summary_ws = [13 * mm, 13 * mm, 13 * mm, 15 * mm, 18 * mm, 16 * mm]
    fixed_w = floor_w + sku_w + sum(summary_ws)
    if n_stage:
        stage_w = max(11 * mm, min(20 * mm, (USABLE_W - fixed_w) / n_stage))
    else:
        stage_w = 0
    # If many stages still push past the page, reclaim width from the SKU column.
    overflow = fixed_w + stage_w * n_stage - USABLE_W
    if overflow > 0:
        sku_w = max(32 * mm, sku_w - overflow)

    col_widths = [floor_w, sku_w] + [stage_w] * n_stage + summary_ws

    # First two columns left-aligned (floor, SKU), the rest right-aligned.
    # The header row sits on a dark-blue background, so it uses white text.
    header_cells = [
        Paragraph(escape(str(v)), _HEADW if i in (0, 1) else _HEADW_R)
        for i, v in enumerate(header)
    ]
    wrapped = (
        [header_cells]
        + [_cells(r, left_cols=(0, 1)) for r in body_rows]
        + [_cells(grand_row, left_cols=(0, 1))]
    )
    tbl = Table(wrapped, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3B6BB0")),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("BOX",        (0, 0), (-1, -1), 0.4, colors.grey),
        ("INNERGRID",  (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F0F0F0")),
        ("LEFTPADDING",   (0, 0), (-1, -1), 2),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 2),
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
        wk_summary, wk_status_list, wk_status_map = await fetch_weekly(conn)
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
        title="Production Module - Entry Counts Report (cumulative, till date)",
    )

    story = []
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    story.append(Paragraph(
        "<b>Production Module - Entry Counts Report</b>", styles["Title"],
    ))
    story.append(Paragraph(
        f"Cumulative snapshot — ALL rows through {generated} UTC<br/>"
        f"Generated {generated} UTC / Database: warehouse_db / Includes v1 + v2 schemas<br/>"
        f"*Effective completion counts JCs with recorded output that are stuck "
        f"short of a terminal status (completion-gate error) as complete.",
        styles["SubHead"],
    ))
    story.append(Spacer(1, 4*mm))
    story.append(build_kpi_tiles(counts))
    story.append(Spacer(1, 5*mm))

    # Row 1: 4 small tables side-by-side
    planning_tbl = build_planning_table(counts)
    po_tbl, po_status_tbl = build_po_tables(counts)
    jc_status_tbl = build_jc_status_table(counts)
    completion_tbl = build_completion_view(counts)
    detail_tbl = build_detail_table(counts)

    row1 = Table(
        [
            [
                Paragraph("<b>Planning</b>", styles["SubHead"]),
                Paragraph("<b>Production orders</b>", styles["SubHead"]),
                Paragraph("<b>Job-card status breakdown</b>", styles["SubHead"]),
                Paragraph("<b>Job-card detail tables</b>", styles["SubHead"]),
            ],
            [planning_tbl,
             [po_tbl, Spacer(1, 1.5*mm), po_status_tbl],
             [jc_status_tbl, Spacer(1, 1.5*mm), completion_tbl],
             detail_tbl],
        ],
        colWidths=[65*mm, 65*mm, 65*mm, 65*mm],
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

    floor_tbl   = build_keyed_table(counts["jc_by_floor"],  ["Key", "Count", "kg"],         [19*mm, 10*mm, 16*mm])
    article_tbl = build_keyed_table(counts["jc_by_article"],["Key", "Count", "kg"],         [31*mm,  9*mm, 16*mm])
    rm_tbl      = build_rm_status(counts["rm_status"])
    stage_tbl   = build_keyed_table(counts["jc_by_stage"],  ["Key", "Count", "kg"],         [25*mm, 10*mm, 16*mm])
    loss_tbl    = build_loss_table(counts["loss_by_cat"])

    row2 = Table(
        [[
            [Paragraph("<b>Floor-wise (JC count + batch kg)</b>", styles["SubHead"]), Spacer(1, 1*mm), floor_tbl],
            [Paragraph("<b>Article-wise (top FG SKUs)</b>",       styles["SubHead"]), Spacer(1, 1*mm), article_tbl],
            [Paragraph("<b>Entry inputs (RM indent status)</b>",  styles["SubHead"]), Spacer(1, 1*mm), rm_tbl],
            [Paragraph("<b>Stage-wise (JC count)</b>",            styles["SubHead"]), Spacer(1, 1*mm), stage_tbl],
            [Paragraph("<b>Loss calculation (by category)</b>",   styles["SubHead"]), Spacer(1, 1*mm), loss_tbl],
        ]],
        colWidths=[50*mm, 61*mm, 52*mm, 56*mm, 52*mm],
    )
    row2.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]))
    # Keep the section heading with its tables so it doesn't dangle at a page break.
    story.append(KeepTogether([
        Paragraph("<b>Operational breakdowns</b>", styles["Heading3"]),
        Spacer(1, 2*mm),
        row2,
    ]))
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph(
        "<i>Counts are point-in-time snapshots from warehouse_db. "
        "Latest = MAX(created_at|updated_at|recorded_at) where present. "
        "*Filled but stuck = job cards with recorded production output "
        "(job_card_output) whose status never reached completed/closed — "
        "typically blocked by the R9 unbalanced-accounting / open-batch "
        "completion gate; counted as effectively complete.</i>",
        styles["Tiny"],
    ))

    # ---- Weekly bifurcation page ----------------------------------------
    story.append(PageBreak())
    story.append(Paragraph("<b>Production Module - Entry Counts Report</b>", styles["Title"]))
    story.append(Paragraph(
        f"Weekly bifurcation — rows bucketed by ISO week (Monday, UTC) of created_at<br/>"
        f"Generated {generated} UTC / Database: warehouse_db / Includes v1 + v2 schemas",
        styles["SubHead"],
    ))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph("<b>Weekly entry counts</b>", styles["Heading3"]))
    story.append(Paragraph(
        "Each row is one week. 'JCs' = job cards created that week. "
        "'Compl' = reached completed/closed; 'Stuck*' = filled (has output) "
        "but not completed; 'Done' = Compl + Stuck (effective). Plans, orders "
        "and indents counted by their own creation week.",
        styles["SubHead"],
    ))
    story.append(Spacer(1, 2*mm))
    story.append(build_weekly_summary(wk_summary))
    story.append(Spacer(1, 6*mm))
    story.append(KeepTogether([
        Paragraph("<b>Weekly job-card status breakdown</b>", styles["Heading3"]),
        Spacer(1, 2*mm),
        build_weekly_status(wk_summary, wk_status_list, wk_status_map),
    ]))

    # ---- Pivot page ------------------------------------------------------
    story.append(PageBreak())
    story.append(Paragraph("<b>Production Module - Entry Counts Report</b>", styles["Title"]))
    story.append(Paragraph(
        f"Cumulative snapshot — ALL rows through {generated} UTC<br/>"
        f"Generated {generated} UTC / Database: warehouse_db",
        styles["SubHead"],
    ))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph("<b>Floor x Article x Stage-of-closure Pivot</b>", styles["Heading3"]))
    story.append(Paragraph(
        "Stage columns count DONE job cards — terminal (completed/closed) OR "
        "filled-but-stuck (has output, completion errored). Summary columns "
        "split done / in-progress / pending.",
        styles["SubHead"],
    ))
    story.append(Spacer(1, 2*mm))

    pivot_tbl, hidden = build_pivot(pivot_rows)
    story.append(pivot_tbl)
    story.append(Spacer(1, 2*mm))
    if hidden:
        story.append(Paragraph(
            f"<i>Hidden {hidden} (floor, FG SKU) combinations with zero done and zero in-progress JCs.</i>",
            styles["Tiny"],
        ))

    doc.build(story)
    print(f"Wrote {OUT_PDF}")


if __name__ == "__main__":
    asyncio.run(main())
