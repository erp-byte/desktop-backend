"""Turn a Savla/Rishi cold storage report into a Stock Take load script.

    & .\\backend\\.venv\\Scripts\\python.exe .\\backend\\scripts\\gen_cold_storage_load.py "<report.xlsx>"

Writes backend/docs/<report day>-cold-storage-opening-stock.sql, which
apply_cold_storage_report.py then dry-runs and applies on RDS. Nothing here writes
to the database; it reads the article catalogue (all_sku) and the article names
earlier loads used, in a READ ONLY transaction.

THE MAPPING (unchanged since the first load, COLD0913):
  * the count day is the report's "Report Date" (its opening stock); entry_id is
    COLD<mmdd> of that day;
  * item_name = the Tally Name as its all_sku article, spelled exactly as earlier
    loads spelled it, so each day's count replaces the last one line for line;
    category / sub category / type come from all_sku; a few Tally Names with no
    article of their own are aliased (ALIASES);
  * warehouse = Unit, floor_name = Storage Location (trimmed); quantity = cartons,
    unit_uom = kg per box, total_weight = kg;
  * Off Grade/Rejection only for OFF-GRADE DATES and -Rej / Burned / Off-grade
    sub groups; everything else Fresh Stock;
  * A-185 Cold is left out (counted on the floor app as A185 / A185 Cold), and so
    are 0 kg lines: the script's step 2 zeroes any article still showing stock
    that the report no longer holds.
A Tally Name that matches no article, or an article the catalogue holds twice
with different categories, stops the run: map it in ALIASES first.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import re
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import asyncpg
import openpyxl

ROOT = Path(__file__).resolve().parent.parent
ENTERED_BY = 'Kaushal'
SKIP_UNITS = {'A-185'}
PLACES = [('D-39', 'Savla'), ('D-514', 'Savla'), ('D-514', 'Savla Bond'), ('Rishi', 'Rishi'), ('Eskimo', 'Eskimo')]
# Tally Names with no all_sku row of their own (precedent: COLD0913 / COLD1019).
ALIASES = {
    'AL BARAKAH FARD DATES PREMIUM A 10KG': 'AL BARAKAH FARD DATES PREMIUM - 10KG',
    'BLUEBERRY - APPLE INFUSED 9.07 KG': 'DRIED BLUEBERRY APPLE INFUSED',
    'FARD DATES SEEDLESS B GRADE': 'AL BARAKAH FARD DATES SEEDLESS',
    'WET DATES SEEDLESS KHALAS(TAJIR RTV)': 'WET DATES SEEDLESS KHALAS',
    'AL BARAKAH CHOPPED DATES 10KG': 'AL BARAKAH CHOPPED DATES',
}
OFF_GRADE = re.compile(r'rej|burn|off-?grade', re.I)


def norm(s):
    return ' '.join(str(s or '').replace('\xa0', ' ').upper().split())


def q(v, places):
    return Decimal(str(v)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def lit(s):
    return "'" + str(s).replace("'", "''") + "'"


def rds_dsn() -> str:
    """Whichever DATABASE_URL line in backend/.env (active or commented out) points at RDS."""
    env = (ROOT / '.env').read_text()
    for dsn in re.findall(r'^\s*#?\s*DATABASE_URL=(postgresql://\S+)', env, re.M):
        if 'rds.amazonaws.com' in dsn.split('@', 1)[-1]:
            return dsn
    sys.exit('no DATABASE_URL line in backend/.env points at RDS')


async def read_catalogue() -> tuple[list[dict], dict[str, str]]:
    """all_sku, and earlier loads' article names keyed by their normalised form."""
    conn = await asyncpg.connect(rds_dsn())
    try:
        async with conn.transaction(readonly=True):
            sku = [dict(r) for r in await conn.fetch(
                'SELECT particulars, item_type, item_group, sub_group FROM all_sku')]
            names = await conn.fetch(
                "SELECT DISTINCT item_name FROM new_stock_entries "
                "WHERE entry_id LIKE 'COLD%' AND total_weight <> 0")
    finally:
        await conn.close()
    return sku, {norm(r['item_name']): r['item_name'] for r in names}


def build(xlsx: Path, sku: list[dict], earlier: dict[str, str]) -> tuple[Path, str, dict]:
    by_sku = collections.defaultdict(list)
    for s in sku:
        by_sku[norm(s['particulars'])].append(s)

    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    ws = next(w for w in wb.worksheets
              if any(r and r[0] == 'Inward Dt' for r in w.iter_rows(max_row=10, values_only=True)))
    rows = list(ws.iter_rows(values_only=True))
    h = next(i for i, r in enumerate(rows) if r and r[0] == 'Inward Dt')
    report_day, as_on = rows[1][1], rows[2][1]      # "Report Date :" / "As On Date :"
    DAY = report_day.date()
    ENTRY_ID = f'COLD{DAY:%m%d}'
    CREATED_AT = f'{DAY:%Y-%m-%d} 09:00:00+05:30'
    AS_ON = f'{as_on:%d-%m-%Y}'
    AUTHORITY = f'Cold storage report (Savla / Rishi / Eskimo) as on {AS_ON} - opening stock {DAY:%d-%m-%Y}'
    OUT = ROOT / 'docs' / f'{DAY:%Y-%m-%d}-cold-storage-opening-stock.sql'
    SRC = xlsx.name

    hdr = rows[h][:26]
    lines, skipped, skipped_zero, problems, all_kg = [], [], [], [], 0.0
    for i, r in enumerate(rows[h + 1:], start=h + 2):
        if not any(r[:26]):
            continue
        d = dict(zip(hdr, r[:26]))
        unit, loc = str(d['Unit']).strip(), str(d['Storage Location']).strip()
        try:
            kg = float(d['Total Inventory Kgs'])
            float(d['Net Qty On Cartons']), float(d['Weight KG/Box'])
        except (TypeError, ValueError):
            problems.append((i, f"non-numeric kg/cartons/kg-per-box: {d['Tally Name']}"))
            continue
        all_kg += kg
        if unit in SKIP_UNITS:
            skipped.append((i, kg))
            continue
        if kg == 0:
            skipped_zero.append(i)
            continue
        key = ALIASES.get(norm(d['Tally Name']), norm(d['Tally Name']))
        hits = by_sku.get(key)
        if not hits:
            problems.append((i, d['Tally Name']))
            continue
        s0 = hits[0]
        if len({(x['item_type'], x['item_group'], x['sub_group']) for x in hits}) > 1:
            problems.append((i, 'ambiguous ' + d['Tally Name']))
        item = earlier.get(key) or ' '.join(str(s0['particulars']).upper().split())
        sub = str(d['Sub Group Name*'] or '')
        off = item == 'OFF-GRADE DATES' or OFF_GRADE.search(sub)
        remark = (f"lot {d['Lot No']} | inward {d['Inward Dt']} | {d['Company Name']} | "
                  f"{d['Group Name*']} / {sub} | {unit} @ {loc}")
        lines.append(dict(
            row=i, item_name=item, item_type=str(s0['item_type']).upper(),
            item_category=str(s0['item_group']).upper(), item_subcategory=str(s0['sub_group']).upper(),
            warehouse=unit, floor_name=loc,
            total_quantity=q(d['Net Qty On Cartons'], 2), unit_uom=q(d['Weight KG/Box'], 3),
            total_weight=q(kg, 2), stock_type='Off Grade/Rejection' if off else 'Fresh Stock',
            remark=remark[:255]))

    if problems:
        sys.exit('cannot map these report lines (sheet row, Tally Name):\n  '
                 + '\n  '.join(f'{r}: {t}' for r, t in problems))
    unknown = {(l['warehouse'], l['floor_name']) for l in lines} - set(PLACES)
    if unknown:
        sys.exit(f'unknown places: {sorted(unknown)}')

    tot = collections.defaultdict(lambda: [0, Decimal(0)])
    for l in lines:
        t = tot[(l['warehouse'], l['floor_name'])]
        t[0] += 1
        t[1] += l['total_weight']
    grand = sum(t[1] for t in tot.values())
    skipped_kg = sum(k for _, k in skipped)
    values = ',\n'.join(
        '    (' + ', '.join([
            str(l['row']), lit(l['item_name']), lit(l['item_type']), lit(l['item_category']),
            lit(l['item_subcategory']), lit(l['warehouse']), lit(l['floor_name']),
            str(l['total_quantity']), str(l['unit_uom']), str(l['total_weight']),
            lit(l['stock_type']), lit(l['remark'])]) + ')'
        for l in lines)
    expected = '\n'.join(f"--   {w:<7} {f:<11} {n:>4} lines  {kg:>12,.2f} kg" for (w, f), (n, kg) in sorted(tot.items()))
    place_values = ', '.join(f"({lit(w.replace('-', '').upper())}, {lit(f.upper())})" for w, f in PLACES)
    place_map = ', '.join(f"({lit(w.replace('-', '').upper())}, {lit(f.upper())}, {lit(w)}, {lit(f)})" for w, f in PLACES)
    off_desc = '; '.join(sorted({f"{l['item_name']} ({l['remark'].split(' | ')[3]})"
                                 for l in lines if l['stock_type'] != 'Fresh Stock'})) or 'none'
    D = f"DATE '{DAY:%Y-%m-%d}'"

    sql = f"""-- Savla / Rishi / Eskimo cold storage OPENING STOCK for {DAY:%d %b %Y} -> Stock Take.
-- Source: {SRC},
--         sheet '{ws.title}' (report dated {DAY:%d-%m-%Y}, stock as on {AS_ON}).
--
-- Run once on RDS warehouse_db, as one script (backend/scripts/apply_cold_storage_report.py).
-- It is a single transaction: if any guard fails, nothing is written.
--
-- WHAT IT WRITES (new_stock_entries only, entry_id '{ENTRY_ID}'), shaped exactly as COLD1019:
--   1. One COUNT row per lot line of the report, dated {DAY:%d %b %Y} 09:00 IST.
--        item_name = Tally Name as the all_sku article (category / sub category / type
--        from all_sku); warehouse = Unit; floor_name = Storage Location; quantity =
--        cartons, unit_uom = kg per box, total_weight = kg.
--        Off Grade/Rejection: {off_desc}.
--   2. A 0 kg COUNT row for every article still showing stock at those five places
--      that is NOT in the report (latest stock is per article per place, so without
--      this an article gone since the last load would keep its old quantity).
--
-- Postings made on {DAY:%d %b} or later at these places are still added on top.
--
-- NOT LOADED:
--   * A-185 Cold ({len(skipped)} lines, {skipped_kg:,.2f} kg) - counted on the floor app.
--   * {len(skipped_zero)} lines at 0 kg (step 2 zeroes any of those articles still showing stock).
--
-- EXPECTED after step 1 ({grand:,.2f} kg):
{expected}

BEGIN;
SET LOCAL lock_timeout = '10s';

-- Refuse to load twice, or on top of a count already made that day at these
-- places: counts on the same day are SUMMED, so either would double stock.
DO $$
DECLARE n bigint;
BEGIN
    SELECT COUNT(*) INTO n FROM new_stock_entries
     WHERE entry_id = '{ENTRY_ID}'
        OR ((created_at AT TIME ZONE 'Asia/Kolkata')::date = {D}
            AND (source_kind IS NULL OR source_kind = 'COUNT')
            AND (REPLACE(UPPER(BTRIM(warehouse)), '-', ''), UPPER(BTRIM(COALESCE(floor_name, ''))))
                IN (VALUES {place_values}));
    IF n > 0 THEN
        RAISE EXCEPTION '% count row(s) already exist for {DAY:%d %b %Y} at the cold stores (or {ENTRY_ID} already loaded) - refusing to double-load', n;
    END IF;
END $$;

CREATE TEMP TABLE cold_load (
    xl_row           int     NOT NULL,
    item_name        text    NOT NULL,
    item_type        text,
    item_category    text,
    item_subcategory text,
    warehouse        text    NOT NULL,
    floor_name       text    NOT NULL,
    total_quantity   numeric NOT NULL,
    unit_uom         numeric NOT NULL,
    total_weight     numeric NOT NULL,
    stock_type       text    NOT NULL,
    remark           text
) ON COMMIT DROP;

-- ALIASES applied (Tally Name -> article):
{chr(10).join(f"--   {k!r} -> {v!r}" for k, v in ALIASES.items())}
INSERT INTO cold_load VALUES
{values};

-- ── 1. The report ─────────────────────────────────────────────────────────
INSERT INTO new_stock_entries (
    item_name, item_type, item_category, item_subcategory,
    warehouse, floor_name, total_quantity, unit_uom, total_weight,
    entered_by, authority, created_at, updated_at,
    stock_type, entry_id, status, is_checked, verified, remark, source_kind
)
SELECT item_name, item_type, item_category, item_subcategory,
       warehouse, floor_name, total_quantity, unit_uom, total_weight,
       '{ENTERED_BY}', '{AUTHORITY}',
       TIMESTAMPTZ '{CREATED_AT}', TIMESTAMPTZ '{CREATED_AT}',
       stock_type, '{ENTRY_ID}', 'submitted', FALSE, FALSE, remark, 'COUNT'
  FROM cold_load
 ORDER BY xl_row;

-- ── 2. Zero what the report no longer holds ───────────────────────────────
-- Balance before {DAY:%d %b}, computed the way latest_stock_service does.
WITH places(k_wh, k_fl) AS (VALUES {place_values}),
cnt AS (
    SELECT UPPER(BTRIM(item_name)) AS k_item,
           COALESCE(stock_type, 'Fresh Stock') AS k_stock,
           REPLACE(UPPER(BTRIM(warehouse)), '-', '') AS k_wh,
           UPPER(BTRIM(COALESCE(floor_name, ''))) AS k_fl,
           (created_at AT TIME ZONE 'Asia/Kolkata')::date AS count_day,
           SUM(total_weight) AS w,
           MIN(item_name) AS item_name, MIN(item_type) AS item_type,
           MIN(item_category) AS item_category, MIN(item_subcategory) AS item_subcategory
      FROM new_stock_entries
     WHERE (source_kind IS NULL OR source_kind = 'COUNT')
       AND (status IS NULL OR status <> 'draft')
       AND (created_at AT TIME ZONE 'Asia/Kolkata')::date < {D}
       AND (REPLACE(UPPER(BTRIM(warehouse)), '-', ''), UPPER(BTRIM(COALESCE(floor_name, ''))))
           IN (SELECT k_wh, k_fl FROM places)
     GROUP BY 1, 2, 3, 4, 5
),
latest AS (
    SELECT DISTINCT ON (k_item, k_stock, k_wh, k_fl) *
      FROM cnt
     ORDER BY k_item, k_stock, k_wh, k_fl, count_day DESC
),
txn AS (
    SELECT UPPER(BTRIM(t.item_name)) AS k_item,
           COALESCE(t.stock_type, 'Fresh Stock') AS k_stock,
           REPLACE(UPPER(BTRIM(t.warehouse)), '-', '') AS k_wh,
           UPPER(BTRIM(COALESCE(t.location, ''))) AS k_fl,
           SUM(CASE WHEN t.operation = 'ADDITION' THEN t.qty_kg ELSE -t.qty_kg END) AS net_kg,
           MIN(t.item_name) AS item_name, MIN(t.material_type) AS item_type,
           MIN(t.item_category) AS item_category, MIN(t.item_subcategory) AS item_subcategory
      FROM stocktake_transactions t
      LEFT JOIN latest l
             ON l.k_item = UPPER(BTRIM(t.item_name))
            AND l.k_stock = COALESCE(t.stock_type, 'Fresh Stock')
            AND l.k_wh = REPLACE(UPPER(BTRIM(t.warehouse)), '-', '')
            AND l.k_fl = UPPER(BTRIM(COALESCE(t.location, '')))
     WHERE (t.created_at AT TIME ZONE 'Asia/Kolkata')::date < {D}
       AND (l.count_day IS NULL OR (t.created_at AT TIME ZONE 'Asia/Kolkata')::date >= l.count_day)
       AND (REPLACE(UPPER(BTRIM(t.warehouse)), '-', ''), UPPER(BTRIM(COALESCE(t.location, ''))))
           IN (SELECT k_wh, k_fl FROM places)
     GROUP BY 1, 2, 3, 4
),
on_hand AS (
    SELECT COALESCE(l.k_item, t.k_item) AS k_item,
           COALESCE(l.k_stock, t.k_stock) AS k_stock,
           COALESCE(l.k_wh, t.k_wh) AS k_wh,
           COALESCE(l.k_fl, t.k_fl) AS k_fl,
           COALESCE(l.item_name, t.item_name) AS item_name,
           COALESCE(l.item_type, t.item_type) AS item_type,
           COALESCE(l.item_category, t.item_category) AS item_category,
           COALESCE(l.item_subcategory, t.item_subcategory) AS item_subcategory,
           COALESCE(l.w, 0) + COALESCE(t.net_kg, 0) AS kg,
           l.count_day
      FROM latest l
      FULL JOIN txn t USING (k_item, k_stock, k_wh, k_fl)
)
INSERT INTO new_stock_entries (
    item_name, item_type, item_category, item_subcategory,
    warehouse, floor_name, total_quantity, unit_uom, total_weight,
    entered_by, authority, created_at, updated_at,
    stock_type, entry_id, status, is_checked, verified, remark, source_kind
)
SELECT o.item_name, o.item_type, o.item_category, o.item_subcategory,
       p.wh, p.fl, 0, 0, 0,
       '{ENTERED_BY}', '{AUTHORITY}',
       TIMESTAMPTZ '{CREATED_AT}', TIMESTAMPTZ '{CREATED_AT}',
       o.k_stock, '{ENTRY_ID}', 'submitted', FALSE, FALSE,
       LEFT('Not in cold storage report as on {AS_ON} - set to 0 (was '
            || ROUND(o.kg, 2) || ' kg, last counted ' || COALESCE(o.count_day::text, 'never') || ')', 255),
       'COUNT'
  FROM on_hand o
  JOIN (VALUES {place_map}) AS p(k_wh, k_fl, wh, fl) ON p.k_wh = o.k_wh AND p.k_fl = o.k_fl
 WHERE ROUND(o.kg, 2) <> 0
   AND NOT EXISTS (
        SELECT 1 FROM cold_load c
         WHERE UPPER(BTRIM(c.item_name)) = o.k_item
           AND c.stock_type = o.k_stock
           AND REPLACE(UPPER(c.warehouse), '-', '') = o.k_wh
           AND UPPER(c.floor_name) = o.k_fl)
 ORDER BY p.wh, p.fl, o.item_name;

-- ── Check: should match EXPECTED in the header (zeroed lines show 0 kg) ──
SELECT warehouse, floor_name,
       COUNT(*) FILTER (WHERE total_weight <> 0) AS report_lines,
       COUNT(*) FILTER (WHERE total_weight = 0)  AS zeroed_articles,
       SUM(total_weight)                          AS kg
  FROM new_stock_entries
 WHERE entry_id = '{ENTRY_ID}'
 GROUP BY 1, 2
 ORDER BY 1, 2;

-- Articles zeroed by step 2, with what they showed before.
SELECT warehouse, floor_name, item_name, stock_type, remark
  FROM new_stock_entries
 WHERE entry_id = '{ENTRY_ID}' AND total_weight = 0
 ORDER BY 1, 2, 3;

-- Loaded article names the live catalogue does not know (expected: none).
SELECT DISTINCT n.item_name
  FROM new_stock_entries n
 WHERE n.entry_id = '{ENTRY_ID}'
   AND NOT EXISTS (SELECT 1 FROM all_sku s
                    WHERE UPPER(BTRIM(s.particulars)) = UPPER(BTRIM(n.item_name)));

COMMIT;
"""
    summary = dict(entry_id=ENTRY_ID, day=DAY, as_on=AS_ON, sheet=ws.title, all_kg=all_kg,
                   lines=len(lines), kg=grand, skipped=len(skipped), skipped_kg=skipped_kg,
                   zero=len(skipped_zero), places=dict(tot), off_grade=off_desc)
    return OUT, sql, summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('report', help='the Savla/Rishi report xlsx')
    ap.add_argument('--out', help='write the SQL here instead of backend/docs/')
    a = ap.parse_args()
    sku, earlier = asyncio.run(read_catalogue())
    out, sql, s = build(Path(a.report), sku, earlier)
    out = Path(a.out) if a.out else out
    out.write_text(sql, encoding='utf-8', newline='\n')
    print(f"{s['entry_id']}  opening stock {s['day']:%d %b %Y} (as on {s['as_on']}), sheet {s['sheet']!r}")
    print(f"report total {s['all_kg']:,.2f} kg = loaded {s['kg']:,.2f} kg ({s['lines']} lines)"
          f" + A-185 Cold {s['skipped_kg']:,.2f} kg ({s['skipped']} lines); {s['zero']} lines at 0 kg")
    for (w, f), (n, kg) in sorted(s['places'].items()):
        print(f"  {w:<7} {f:<11} {n:>4} lines  {kg:>12,.2f} kg")
    print(f"off grade: {s['off_grade']}")
    print(f"written: {out}")


if __name__ == '__main__':
    main()
