"""Sync FG Master spreadsheet → bom_header + bom_process_route.

Reads docs/FG_Master_Completion (1) (1).xlsx (new column layout with the
Bar Line Process column inserted at column F) and updates the production
database non-destructively:

  * bom_header rows are matched by (fg_sku_name, customer_name IS NULL).
  * Missing FG rows are INSERTed.
  * For existing rows, only NULL/empty fields are filled in (COALESCE
    semantics). process_category and bar_line_process are always synced
    from the Excel because the user explicitly asked for process updates.
  * bom_process_route steps are INSERTed where missing — never deleted.

Usage:

    python scripts/sync_fg_master.py --dry-run
    python scripts/sync_fg_master.py --apply

The dry-run mode prints exactly what would change without touching the DB.
"""

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

import asyncpg
import openpyxl
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

EXCEL_PATH = Path(__file__).parent.parent / "docs" / "FG_Master_Completion (1) (1).xlsx"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_str(val) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        return round(float(val), 3)
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> int | None:
    if val is None:
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def _split_comma(val) -> list[str]:
    if not val:
        return []
    return [p.strip() for p in str(val).split(",") if p.strip()]


def _split_process_category(val) -> list[str]:
    if not val:
        return []
    s = str(val)
    s = re.sub(r'[  \s]*\+[  \s]*', '+', s)
    parts = [p.strip().strip('()') for p in s.split('+')]
    return [p for p in parts if p]


def _derive_entity(factory: str | None) -> str | None:
    if not factory:
        return None
    f = str(factory).strip().upper()
    if 'W202' in f:
        return 'cfpl'
    if 'A185' in f:
        return 'cdpl'
    return None


# ---------------------------------------------------------------------------
# Excel parsing
# ---------------------------------------------------------------------------
# Column layout for FG_Master_Completion (1) (1).xlsx — header row is row 2,
# data starts at row 3 (row 3 is a "Particluars" template row which is
# skipped). 0-based indices below:
#
#   0  Sr No
#   1  FG Name
#   2  Group
#   3  Sub-Group
#   4  Process Category
#   5  Bar Line Process            <-- NEW column F
#   6  BU
#   7  Factory (W202/A185)
#   8  Floors (comma-sep)
#   9  Machines (comma-sep)
#   10 Net Weight/Unit (Kg)
#   11 Shelf Life (Days)
#   12 GST Rate
#   13 HSN/SAC
#   14 Inventory Group
#   15 Customer Code
#   16 Status

def parse_excel(path: Path) -> list[dict]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb['FG_Master_Fill']

    rows: list[dict] = []
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        if i <= 2:  # row 1 = "PRE-FILLED" banner, row 2 = headers
            continue

        vals = list(row)
        while len(vals) < 17:
            vals.append(None)

        fg_name = _safe_str(vals[1])
        if not fg_name or fg_name == 'Particluars':
            continue

        rows.append({
            'fg_name': fg_name,
            'group': _safe_str(vals[2]),
            'sub_group': _safe_str(vals[3]),
            'process_category': _safe_str(vals[4]),
            'bar_line_process': _safe_str(vals[5]),
            'business_unit': _safe_str(vals[6]),
            'factory': _safe_str(vals[7]),
            'floors': _split_comma(vals[8]),
            'machines': _split_comma(vals[9]),
            'pack_size_kg': _safe_float(vals[10]),
            'shelf_life_days': _safe_int(vals[11]),
            'gst_rate': _safe_float(vals[12]),
            'hsn_sac': _safe_str(vals[13]),
            'inventory_group': _safe_str(vals[14]),
            'customer_code': _safe_str(vals[15]),
        })

    wb.close()
    return rows


# ---------------------------------------------------------------------------
# DB sync
# ---------------------------------------------------------------------------

# Fields that follow COALESCE semantics — only filled when the existing DB
# value is NULL. Excel-empty values never overwrite DB values.
_COALESCE_FIELDS = (
    'sub_group', 'business_unit', 'factory', 'pack_size_kg',
    'shelf_life_days', 'gst_rate', 'hsn_sac', 'inventory_group',
    'customer_code', 'item_group', 'entity',
)

# Array fields — COALESCE when the existing DB array is NULL.
_COALESCE_ARRAY_FIELDS = ('floors', 'machines')

# Fields that are always synced from the Excel ("processes need updating"),
# but only if the Excel value is non-NULL — we never overwrite DB with NULL.
_FORCE_FIELDS = ('process_category', 'bar_line_process')


class SyncStats:
    def __init__(self):
        self.headers_inserted = 0
        self.headers_updated = 0
        self.headers_unchanged = 0
        self.routes_inserted = 0
        self.routes_skipped = 0
        self.missing_columns: list[str] = []
        self.has_bar_line_col = False


async def ensure_column(conn, *, apply: bool, stats: SyncStats):
    """Verify the bar_line_process column exists; create it if --apply."""
    has_col = await conn.fetchval(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'bom_header' AND column_name = 'bar_line_process'
        """
    )
    if has_col:
        stats.has_bar_line_col = True
        return

    if apply:
        logger.info("Adding bom_header.bar_line_process column ...")
        await conn.execute("ALTER TABLE bom_header ADD COLUMN bar_line_process TEXT")
        stats.has_bar_line_col = True
    else:
        stats.missing_columns.append('bom_header.bar_line_process')
        logger.warning(
            "bom_header.bar_line_process column missing — would be created on --apply"
        )


async def sync_row(conn, row: dict, *, apply: bool, stats: SyncStats):
    fg_name = row['fg_name']

    bar_line_select = "bar_line_process" if stats.has_bar_line_col else "NULL::TEXT AS bar_line_process"
    existing = await conn.fetchrow(
        f"""
        SELECT bom_id, sub_group, process_category, business_unit, factory,
               floors, machines, pack_size_kg, shelf_life_days, gst_rate,
               hsn_sac, inventory_group, customer_code, item_group, entity,
               {bar_line_select}
        FROM bom_header
        WHERE fg_sku_name = $1 AND customer_name IS NULL
        ORDER BY bom_id
        LIMIT 1
        """,
        fg_name,
    )

    entity = _derive_entity(row['factory'])
    item_group = row['group']

    if existing is None:
        if apply:
            bom_id = await conn.fetchval(
                """
                INSERT INTO bom_header (
                    fg_sku_name, customer_name, pack_size_kg, item_group, entity,
                    sub_group, process_category, business_unit, factory,
                    floors, machines, shelf_life_days, gst_rate, hsn_sac,
                    inventory_group, customer_code, bar_line_process
                ) VALUES (
                    $1, NULL, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16
                )
                RETURNING bom_id
                """,
                fg_name, row['pack_size_kg'], item_group, entity,
                row['sub_group'], row['process_category'], row['business_unit'],
                row['factory'], row['floors'] or None, row['machines'] or None,
                row['shelf_life_days'], row['gst_rate'], row['hsn_sac'],
                row['inventory_group'], row['customer_code'], row['bar_line_process'],
            )
        else:
            bom_id = None
        stats.headers_inserted += 1
        logger.info("INSERT %s", fg_name)
        await sync_routes(conn, bom_id, fg_name, row, apply=apply, stats=stats)
        return

    bom_id = existing['bom_id']

    updates: list[tuple[str, object]] = []
    excel_values = {
        'sub_group': row['sub_group'],
        'business_unit': row['business_unit'],
        'factory': row['factory'],
        'floors': row['floors'] or None,
        'machines': row['machines'] or None,
        'pack_size_kg': row['pack_size_kg'],
        'shelf_life_days': row['shelf_life_days'],
        'gst_rate': row['gst_rate'],
        'hsn_sac': row['hsn_sac'],
        'inventory_group': row['inventory_group'],
        'customer_code': row['customer_code'],
        'item_group': item_group,
        'entity': entity,
        'process_category': row['process_category'],
        'bar_line_process': row['bar_line_process'],
    }

    for field in _COALESCE_FIELDS:
        excel_val = excel_values[field]
        if excel_val is not None and existing[field] is None:
            updates.append((field, excel_val))

    for field in _COALESCE_ARRAY_FIELDS:
        excel_val = excel_values[field]
        if excel_val and not existing[field]:
            updates.append((field, excel_val))

    for field in _FORCE_FIELDS:
        excel_val = excel_values[field]
        if excel_val is not None and excel_val != existing[field]:
            updates.append((field, excel_val))

    if updates:
        if apply:
            set_clauses = ", ".join(f"{f} = ${i + 2}" for i, (f, _) in enumerate(updates))
            args = [bom_id] + [v for _, v in updates]
            await conn.execute(
                f"UPDATE bom_header SET {set_clauses} WHERE bom_id = $1",
                *args,
            )
        stats.headers_updated += 1
        changed = ", ".join(f for f, _ in updates)
        logger.info("UPDATE %s [%s]", fg_name, changed)
    else:
        stats.headers_unchanged += 1

    await sync_routes(conn, bom_id, fg_name, row, apply=apply, stats=stats)


async def sync_routes(conn, bom_id: int | None, fg_name: str, row: dict, *,
                       apply: bool, stats: SyncStats):
    """INSERT bom_process_route steps that don't already exist for this bom_id.

    Source of routes: process_category column (the existing convention).
    Never deletes or renumbers existing steps.
    """
    steps = _split_process_category(row['process_category'])
    if not steps:
        return

    if bom_id is None:
        # Dry-run for a new FG header — no bom_id yet; just count.
        stats.routes_inserted += len(steps)
        return

    existing_steps = {
        r['step_number']: r['process_name']
        for r in await conn.fetch(
            "SELECT step_number, process_name FROM bom_process_route WHERE bom_id = $1",
            bom_id,
        )
    }

    for step_num, step_name in enumerate(steps, 1):
        if step_num in existing_steps:
            stats.routes_skipped += 1
            continue
        if apply:
            stage = step_name.lower().replace(' ', '_')
            await conn.execute(
                """
                INSERT INTO bom_process_route (bom_id, step_number, process_name, stage)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (bom_id, step_number) DO NOTHING
                """,
                bom_id, step_num, step_name, stage,
            )
        stats.routes_inserted += 1
        logger.debug("  + route step %d: %s", step_num, step_name)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--dry-run', action='store_true',
                       help='Print intended changes without touching DB')
    group.add_argument('--apply', action='store_true',
                       help='Apply changes to the database')
    parser.add_argument('--excel', default=str(EXCEL_PATH),
                        help='Path to FG_Master_Completion xlsx')
    args = parser.parse_args()

    apply = args.apply
    excel_path = Path(args.excel)

    if not excel_path.exists():
        logger.error("Excel file not found: %s", excel_path)
        sys.exit(1)

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        logger.error('DATABASE_URL not set')
        sys.exit(1)

    logger.info('Mode: %s', 'APPLY' if apply else 'DRY-RUN')
    logger.info('Excel: %s', excel_path)

    rows = parse_excel(excel_path)
    logger.info('Parsed %d FG rows', len(rows))

    conn = await asyncpg.connect(database_url)
    stats = SyncStats()
    try:
        async with conn.transaction():
            await ensure_column(conn, apply=apply, stats=stats)
            for row in rows:
                await sync_row(conn, row, apply=apply, stats=stats)
            if not apply:
                # Roll back any inadvertent writes in dry-run mode.
                raise _DryRunRollback
    except _DryRunRollback:
        pass
    finally:
        await conn.close()

    logger.info('--- SUMMARY (%s) ---', 'APPLY' if apply else 'DRY-RUN')
    logger.info('bom_header  inserts:   %d', stats.headers_inserted)
    logger.info('bom_header  updates:   %d', stats.headers_updated)
    logger.info('bom_header  unchanged: %d', stats.headers_unchanged)
    logger.info('process_route inserts: %d', stats.routes_inserted)
    logger.info('process_route already-present: %d', stats.routes_skipped)
    if stats.missing_columns:
        logger.warning('Missing columns (created on --apply): %s', stats.missing_columns)


class _DryRunRollback(Exception):
    """Sentinel to abort the transaction without committing in dry-run mode."""


if __name__ == '__main__':
    asyncio.run(main())
