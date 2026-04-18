"""Master data ingest — BOM headers, BOM lines, process routes, machines.

Reads three Excel files at startup and populates:
  - bom_header       (from FG_Master_Completion.xlsx)
  - bom_process_route (derived from Process Category column)
  - bom_line          (from BOM_Enrichment.xlsx)
  - machine           (from Floorwise utility dada.xlsx)

Idempotent: skips if bom_header already has data.
"""

import logging
import re
from pathlib import Path

import openpyxl

from app.modules.so.services.item_matcher import MasterItem, match_sku

logger = logging.getLogger(__name__)

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
    """Split comma-separated string, strip each part, remove empties."""
    if not val:
        return []
    return [p.strip() for p in str(val).split(",") if p.strip()]


def _split_process_category(val) -> list[str]:
    """Split process category by '+', handling unicode thin spaces and parens."""
    if not val:
        return []
    s = str(val)
    # Normalize unicode thin/non-breaking spaces around +
    s = re.sub(r'[\u2009\u00a0\s]*\+[\u2009\u00a0\s]*', '+', s)
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
# 1. FG Master → bom_header + bom_process_route
# ---------------------------------------------------------------------------

async def ingest_fg_master(conn, file_path: Path, master_items: list[MasterItem]) -> dict:
    """Read FG_Master_Fill sheet → bom_header + bom_process_route. Returns header_map."""

    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    ws = wb['FG_Master_Fill']

    header_map: dict[tuple[str, str | None], int] = {}  # (fg_name, variant) → bom_id
    headers_created = 0
    routes_created = 0
    skipped = 0

    for i, row in enumerate(ws.iter_rows(values_only=True)):
        # Row 0 = header, Row 1 = template row ("Particluars")
        if i < 2:
            continue

        vals = list(row)
        while len(vals) < 16:
            vals.append(None)

        fg_name = _safe_str(vals[1])
        if not fg_name or fg_name == 'Particluars':
            skipped += 1
            continue

        group = _safe_str(vals[2])
        sub_group = _safe_str(vals[3])
        process_category = _safe_str(vals[4])
        business_unit = _safe_str(vals[5])
        factory = _safe_str(vals[6])
        floors = _split_comma(vals[7])
        machines = _split_comma(vals[8])
        pack_size_kg = _safe_float(vals[9])
        shelf_life_days = _safe_int(vals[10])
        gst_rate = _safe_float(vals[11])
        hsn_sac = _safe_str(vals[12])
        inventory_group = _safe_str(vals[13])
        customer_code = _safe_str(vals[14])

        entity = _derive_entity(factory)

        # Use Excel Group as item_group; fallback to fuzzy match
        item_group = group
        if not item_group and master_items:
            matched, _score = match_sku(fg_name, master_items)
            if matched:
                item_group = matched.group

        bom_id = await conn.fetchval(
            """
            INSERT INTO bom_header (
                fg_sku_name, customer_name, pack_size_kg, item_group,
                entity, sub_group, process_category, business_unit,
                factory, floors, machines, shelf_life_days,
                gst_rate, hsn_sac, inventory_group, customer_code
            ) VALUES ($1, NULL, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            ON CONFLICT DO NOTHING
            RETURNING bom_id
            """,
            fg_name, pack_size_kg, item_group,
            entity, sub_group, process_category, business_unit,
            factory, floors or None, machines or None, shelf_life_days,
            gst_rate, hsn_sac, inventory_group, customer_code,
        )

        if bom_id is None:
            # Already exists — fetch existing id
            bom_id = await conn.fetchval(
                "SELECT bom_id FROM bom_header WHERE fg_sku_name = $1 AND customer_name IS NULL LIMIT 1",
                fg_name,
            )
            if bom_id is None:
                skipped += 1
                continue
        else:
            headers_created += 1

        header_map[(fg_name, None)] = bom_id

        # Process route from Process Category (split by +)
        steps = _split_process_category(process_category)
        for step_num, step_name in enumerate(steps, 1):
            stage = step_name.lower().replace(' ', '_')
            await conn.execute(
                """
                INSERT INTO bom_process_route (bom_id, step_number, process_name, stage)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (bom_id, step_number) DO NOTHING
                """,
                bom_id, step_num, step_name, stage,
            )
            routes_created += 1

    wb.close()
    logger.info("FG Master: %d headers, %d routes, %d skipped", headers_created, routes_created, skipped)
    return header_map


# ---------------------------------------------------------------------------
# 2. BOM Enrichment → bom_line
# ---------------------------------------------------------------------------

async def ingest_bom_lines(
    conn, file_path: Path, header_map: dict, master_items: list[MasterItem],
) -> dict:
    """Read BOM_Enrichment sheet → bom_line."""

    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    ws = wb['BOM_Enrichment']

    lines_created = 0
    matched = 0
    unmatched = 0
    skipped = 0
    line_counters: dict[int, int] = {}  # bom_id → next line_number

    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i < 1:  # header row
            continue

        vals = list(row)
        while len(vals) < 13:
            vals.append(None)

        fg_name = _safe_str(vals[1])
        if not fg_name:
            skipped += 1
            continue

        variant = _safe_str(vals[2])  # BOM Variant — NULL = default
        component = _safe_str(vals[3])
        mat_type = _safe_str(vals[4])
        godown = _safe_str(vals[5])
        uom = _safe_str(vals[6])
        qty_per_unit = _safe_float(vals[7])
        loss_pct = _safe_float(vals[8])
        unit_rate = _safe_float(vals[9])
        process_stage_raw = _safe_str(vals[10])

        if not component or qty_per_unit is None:
            skipped += 1
            continue

        # Clean process_stage
        process_stage = process_stage_raw
        if process_stage and 'will be done' in process_stage.lower():
            process_stage = None

        # Lookup bom_id
        key = (fg_name, variant)
        bom_id = header_map.get(key)
        if bom_id is None and variant is not None:
            # Try default BOM
            bom_id = header_map.get((fg_name, None))
        if bom_id is None:
            skipped += 1
            continue

        # Line number
        line_num = line_counters.get(bom_id, 0) + 1
        line_counters[bom_id] = line_num

        # Normalize item_type
        item_type = mat_type.lower() if mat_type else 'rm'

        # Fuzzy match component against all_sku
        if master_items:
            mi, score = match_sku(component, master_items)
            if mi:
                matched += 1
            else:
                unmatched += 1

        await conn.execute(
            """
            INSERT INTO bom_line (
                bom_id, line_number, material_sku_name, item_type,
                quantity_per_unit, uom, loss_pct, godown,
                unit_rate_inr, process_stage
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (bom_id, line_number) DO NOTHING
            """,
            bom_id, line_num, component, item_type,
            qty_per_unit, uom, loss_pct or 0, godown,
            unit_rate, process_stage,
        )
        lines_created += 1

    wb.close()
    logger.info("BOM Lines: %d created, %d matched, %d unmatched, %d skipped",
                lines_created, matched, unmatched, skipped)
    return {"lines_created": lines_created, "matched": matched, "unmatched": unmatched}


# ---------------------------------------------------------------------------
# 3. Floorwise machine list → machine
# ---------------------------------------------------------------------------

async def ingest_machines(conn, file_path: Path) -> dict:
    """Read floorwise machine list → machine table. Inserts ALL machines."""

    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    ws = wb['floorwise machine list']

    machines_created = 0
    current_area = None

    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:  # header row
            continue

        vals = list(row)
        while len(vals) < 4:
            vals.append(None)

        area = _safe_str(vals[0])
        machine_name = _safe_str(vals[1])

        if area and area != 'Area':
            current_area = area

        if machine_name and not area:
            # This is a machine under current_area
            await conn.execute(
                """
                INSERT INTO machine (machine_name, floor, factory, entity, allocation)
                VALUES ($1, $2, 'W202', 'cfpl', 'idle')
                """,
                machine_name, current_area,
            )
            machines_created += 1

    wb.close()
    logger.info("Machines: %d created", machines_created)
    return {"machines_created": machines_created}


# ---------------------------------------------------------------------------
# 4. Derive machine_capacity from FG Master (machine × stage × group)
# ---------------------------------------------------------------------------

# Default kg/hr rates by stage — reasonable food processing industry defaults.
# These are starting estimates; editable later via API.
_DEFAULT_CAPACITY_BY_STAGE = {
    'sorting':                          150,
    'sorting (bulk packaging':          200,
    'sorting (bulk packaging)':         200,
    'roasting':                         100,
    'roasting (bulk packaging':         120,
    'roasting (bulk packaging)':        120,
    'packaging':                        120,
    'flavouring':                       80,
    'flavouring (bulk packaging':       100,
    'flavouring (bulk packaging)':      100,
    'blanching':                        100,
    'slicing/dicing/slivering':         60,
    'slicing/dicing/slivering (bulk packaging': 80,
    'slicing/dicing/slivering (bulk packaging)': 80,
    'de-seeding':                       50,
    'stuffing':                         40,
    'blending':                         80,
    'bar forming':                      60,
    'chocolate':                        50,
    'chocolate (bulk packaging':        60,
    'chocolate (bulk packaging)':       60,
}
_DEFAULT_FALLBACK = 80  # kg/hr when stage not in table

# Multipliers per product group — heavier/harder items are slower
_GROUP_MULTIPLIER = {
    'DATES':            1.2,   # heavy, sticky
    'CASHEW':           1.0,
    'ALMOND':           0.95,
    'PISTA':            0.9,
    'PEANUTS':          1.1,   # small, fast
    'RAISIN':           0.9,
    'SEEDS':            1.0,
    'TRAIL MIX':        0.85,  # mixed, slower
    'BARS & CEREALS':   0.7,   # complex processing
    'FESTIVE HAMPERS':  0.5,   # manual assembly
    'CRANBERRY':        0.9,
    'ANJEER':           0.8,
    'APRICOT':          0.85,
    'BLUEBERRY':        0.85,
    'MAKHANA':          1.0,
    'PREMIUM NUTS':     0.9,
    'PRUNES':           0.85,
    'WALNUT':           0.9,
    'DEHYDRATED FRUITS': 0.9,
    'BLACKCURRANT':     0.85,
    'BLACKBERRY':       0.85,
    'TAJIR':            1.0,
    'SPICES':           0.6,
}


async def derive_machine_capacity(conn, fg_master_path: Path) -> dict:
    """Derive machine_capacity from FG Master machine×stage×group mappings.

    Uses default kg/hr rates by stage, adjusted by product group multiplier.
    """
    wb = openpyxl.load_workbook(fg_master_path, read_only=True, data_only=True)
    ws = wb['FG_Master_Fill']

    # Collect unique (machine_name, stage, group) combos
    combos: set[tuple[str, str, str]] = set()

    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i < 2:
            continue
        vals = list(row)
        while len(vals) < 16:
            vals.append(None)

        fg_name = _safe_str(vals[1])
        group = _safe_str(vals[2])
        process_cat = _safe_str(vals[4])
        machines_raw = _safe_str(vals[8])

        if not fg_name or fg_name == 'Particluars':
            continue
        if not machines_raw or not process_cat or not group:
            continue

        machines = _split_comma(machines_raw)
        stages = _split_process_category(process_cat)

        for m in machines:
            for s in stages:
                combos.add((m, s, group))

    wb.close()

    # Build machine name → machine_id lookup from DB
    rows = await conn.fetch("SELECT machine_id, machine_name FROM machine")
    # Normalize lookup: lowercase stripped
    machine_lookup: dict[str, int] = {}
    for r in rows:
        key = r['machine_name'].strip().lower()
        machine_lookup[key] = r['machine_id']

    capacity_created = 0
    skipped = 0

    for machine_name, stage_name, group in combos:
        machine_key = machine_name.strip().lower()
        machine_id = machine_lookup.get(machine_key)
        if machine_id is None:
            skipped += 1
            continue

        stage_lower = stage_name.lower().strip()
        base_rate = _DEFAULT_CAPACITY_BY_STAGE.get(stage_lower, _DEFAULT_FALLBACK)
        multiplier = _GROUP_MULTIPLIER.get(group, 0.9)
        capacity = round(base_rate * multiplier, 3)

        await conn.execute(
            """
            INSERT INTO machine_capacity (machine_id, stage, item_group, capacity_kg_per_hr)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (machine_id, stage, item_group) DO NOTHING
            """,
            machine_id, stage_lower, group, capacity,
        )
        capacity_created += 1

    logger.info("Machine capacity: %d entries created, %d skipped (machine not in DB)", capacity_created, skipped)
    return {"capacity_created": capacity_created, "skipped": skipped}


# ---------------------------------------------------------------------------
# Stock Ingest (Physical Stock + A-185 Stock Take → floor_inventory)
# ---------------------------------------------------------------------------

# Map Excel location columns to floor_location values
# Physical Stock CFPL sheet: pairs of (Qty, KG) columns starting at col 6
_CFPL_LOCATIONS = [
    (6, 7, "rm_store"),           # W-202 Store
    (8, 9, "production_floor"),   # W-202 Lower Basement
    (10, 11, "production_floor"), # W-202 Upper Basement
    (12, 13, "production_floor"), # W-202 1st Floor
    (14, 15, "production_floor"), # W-202 2nd Floor
    (16, 17, "production_floor"), # W-202 Barline
    (18, 19, "production_floor"), # W-202 Terrace
    (20, 21, "offgrade"),         # W-202 Off-Grade
]

# A-185 Stock Take Consolidated sheet: pairs of (Qty, KG) starting at col 6
_A185_LOCATIONS = [
    (6, 7, "rm_store"),           # Rack & Dock Area
    (8, 9, "production_floor"),   # Production
    (10, 11, "production_floor"), # Packing Area
    (12, 13, "fg_store"),         # Cold Area
    (14, 15, "offgrade"),         # Off-Grade
]


async def ingest_stock(conn, data_dir: Path) -> dict:
    """Ingest stock from Physical Stock.xlsx (CFPL) + A-185 Stock Take.xlsx (A-185).

    Populates floor_inventory table. Idempotent — skips if floor_inventory has data.
    """
    count = await conn.fetchval("SELECT COUNT(*) FROM floor_inventory")
    if count and count > 0:
        return {"status": "already_ingested", "count": count}

    inserted = 0

    # --- Physical Stock (CFPL) ---
    cfpl_file = data_dir / "Physical Stock.xlsx"
    if cfpl_file.exists():
        wb = openpyxl.load_workbook(cfpl_file, read_only=True, data_only=True)
        ws = wb['CFPL']
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i < 3:  # skip header rows (row 1 = totals, row 2 = locations, row 3 = column headers)
                continue
            vals = list(row)
            while len(vals) < 40:
                vals.append(None)

            item_name = _safe_str(vals[1])
            fg_rm = _safe_str(vals[2])
            group = _safe_str(vals[3])
            pack_size = vals[5]

            if not item_name:
                continue

            item_type = 'rm' if fg_rm and fg_rm.upper() == 'RM' else 'fg'

            for qty_col, kg_col, floor_loc in _CFPL_LOCATIONS:
                kg_val = vals[kg_col] if kg_col < len(vals) else None
                if kg_val and isinstance(kg_val, (int, float)) and kg_val > 0:
                    await conn.execute(
                        """
                        INSERT INTO floor_inventory (sku_name, item_type, floor_location, quantity_kg, entity)
                        VALUES ($1, $2, $3, $4, 'cfpl')
                        ON CONFLICT (sku_name, floor_location, lot_number, entity)
                        DO UPDATE SET quantity_kg = floor_inventory.quantity_kg + $4, last_updated = NOW()
                        """,
                        item_name, item_type, floor_loc, float(kg_val),
                    )
                    inserted += 1

        wb.close()

    # --- A-185 Stock Take ---
    a185_file = data_dir / "A-185 Stock Take.xlsx"
    if a185_file.exists():
        wb = openpyxl.load_workbook(a185_file, read_only=True, data_only=True)
        ws = wb['Consolidated']
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i < 3:
                continue
            vals = list(row)
            while len(vals) < 16:
                vals.append(None)

            item_name = _safe_str(vals[1])
            fg_rm = _safe_str(vals[2])

            if not item_name:
                continue

            item_type = 'rm' if fg_rm and fg_rm.upper() == 'RM' else 'fg'

            for qty_col, kg_col, floor_loc in _A185_LOCATIONS:
                kg_val = vals[kg_col] if kg_col < len(vals) else None
                if kg_val and isinstance(kg_val, (int, float)) and kg_val > 0:
                    await conn.execute(
                        """
                        INSERT INTO floor_inventory (sku_name, item_type, floor_location, quantity_kg, entity)
                        VALUES ($1, $2, $3, $4, 'cdpl')
                        ON CONFLICT (sku_name, floor_location, lot_number, entity)
                        DO UPDATE SET quantity_kg = floor_inventory.quantity_kg + $4, last_updated = NOW()
                        """,
                        item_name, item_type, floor_loc, float(kg_val),
                    )
                    inserted += 1

        wb.close()

    logger.info("Stock ingest: %d inventory entries created", inserted)
    return {"inserted": inserted}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run_master_ingest(pool, data_dir: Path, master_items: list) -> dict | None:
    """Run all master data ingests. Idempotent — skips if data already exists."""

    count = await pool.fetchval("SELECT COUNT(*) FROM bom_header")
    if count and count > 0:
        # Check if machine_capacity needs backfill
        cap_count = await pool.fetchval("SELECT COUNT(*) FROM machine_capacity")
        if cap_count == 0:
            fg_file = data_dir / "FG_Master_Completion.xlsx"
            if fg_file.exists():
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        cap_result = await derive_machine_capacity(conn, fg_file)
                logger.info("Backfilled machine capacity: %d entries", cap_result["capacity_created"])

        # Check if stock needs ingest
        stock_count = await pool.fetchval("SELECT COUNT(*) FROM floor_inventory")
        if stock_count == 0:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    stock_result = await ingest_stock(conn, data_dir)
                logger.info("Backfilled stock: %s", stock_result)
        else:
            logger.info("Master data already ingested (%d headers, %d capacity, %d stock), skipping",
                         count, cap_count, stock_count)
        return None

    fg_file = data_dir / "FG_Master_Completion.xlsx"
    bom_file = data_dir / "BOM_Enrichment.xlsx"
    machine_file = data_dir / "Floorwise utility dada.xlsx"

    for f in [fg_file, bom_file, machine_file]:
        if not f.exists():
            logger.warning("Master ingest skipped — file not found: %s", f)
            return None

    async with pool.acquire() as conn:
        async with conn.transaction():
            header_map = await ingest_fg_master(conn, fg_file, master_items)
            bom_result = await ingest_bom_lines(conn, bom_file, header_map, master_items)
            machine_result = await ingest_machines(conn, machine_file)
            capacity_result = await derive_machine_capacity(conn, fg_file)
            stock_result = await ingest_stock(conn, data_dir)

    logger.info(
        "Master data ingest complete: %d headers, %d BOM lines, %d machines, %d capacity, %d stock entries",
        len(header_map), bom_result["lines_created"],
        machine_result["machines_created"], capacity_result["capacity_created"],
        stock_result["inserted"],
    )
    return {
        "headers": len(header_map),
        "bom_lines": bom_result["lines_created"],
        "machines": machine_result["machines_created"],
        "capacity_entries": capacity_result["capacity_created"],
        "stock_entries": stock_result["inserted"],
    }
