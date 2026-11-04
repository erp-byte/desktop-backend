"""A cold return's cartons in cold stock.

A customer return whose factory unit is a cold store (Savla D-39 / D-514 / Bond, Rishi,
Supreme, Eskimo: app.core.sites.cold_company) brings its cartons back into that store.
Once it is approved, every printed box becomes one row of the return company's cold
stock ({cfpl,cdpl}_cold_stocks), as IMS's rtv_tools.sync_cold_stocks_from_rtv did until
IMS closed on 2026-10-19. The cold-storage screens, inner cold transfers and Scan & send
then find the carton by its label.

IMS deleted and re-made a return's rows on every box save. Here a row is made once and
after that only corrected, because a cold transfer may have relabelled or moved it since:
  * when: on approval (approval_service.apply_cr_action) and on every box print or save
    after it (box_service), inside that write's transaction and under the return's box
    lock (query_service.lock_box_writes). A box printed while the approval lands is
    neither missed nor copied twice;
  * which boxes: the printed ones (a box id) that the ERP wrote, i.e. created on or
    after IMS_CLOSED. A box IMS wrote is never copied: IMS copied it itself, and if that
    row is gone, a transfer took the carton away. A return that has IMS's rows
    (ims_rows) is left alone, and box_service keeps its boxes as they are;
  * every row made is recorded in cr_cold_stock_rows (migration 135) together with what
    was copied from the box. A later edit of the box's weight, lot, mark, vakkal or
    remarks is carried to its row. Nothing else is touched, so an inner cold transfer's
    relabel or move stays. A recorded box whose row has gone is not made again;
  * the row: one carton, weight_kg = total_inventory_kgs = the box's net weight, value =
    net weight x the line's rate; lot, mark, vakkal and remarks from the box, else from
    its line; inward_dt the return's IST date; unit the store's code (D-39, Rishi) and
    storage_location its name, as IMS wrote them; box_id with transaction_no = the CR
    number (the label's QR), and inward_transaction_no = the CR number with
    auto_created_from_inward, IMS's marks of a return's row (box_identify_service reads
    inward_transaction_no to count the label and the row as one carton).
Nothing is copied, and a warning is logged, while migration 135 is missing or where the
cold table is absent or lacks a column the row needs (a dev database). Apply 135 before
the deploy: a return approved without it is copied only at its next box print or save.
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from app.core.sites import canonical_site, cold_company, grantable_codes, same_site
from app.modules.customer_returns.services import query_service as q
from app.modules.customer_returns.tables import cr_table_names

logger = logging.getLogger(__name__)

# The day IMS closed. Every box of a cold return written before it was written by IMS.
IMS_CLOSED = datetime(2026, 10, 19, tzinfo=q.IST)
LINKS = "cr_cold_stock_rows"
# The columns every cold-stock row is written with (as inner_cold_service writes one),
# and those written only where the table has them: cold tables differ across databases.
_NEEDED = frozenset({
    "id", "inward_dt", "unit", "inward_no", "item_description", "item_mark", "vakkal", "lot_no",
    "no_of_cartons", "weight_kg", "total_inventory_kgs", "group_name", "item_subgroup",
    "storage_location", "exporter", "last_purchase_rate", "value", "box_id", "transaction_no"})
# What a row copies from its box, as recorded in cr_cold_stock_rows.
_COPIED = ("net_weight", "lot_no", "item_mark", "vakkal", "spl_remarks")
# IMS's marks of a row made from a return; a table without them holds none of IMS's.
_IMS_MARKS = frozenset({"inward_transaction_no", "auto_created_from_inward"})


def cold_store(factory_unit) -> str | None:
    """The cold store a return's factory unit names, by its canonical name; None for a
    warehouse, an unknown name and 'Cold Storage' (which is no one store)."""
    raw = str(factory_unit) if factory_unit else None
    return canonical_site(raw) if cold_company(raw) else None


def _unit(store: str) -> str:
    """The store's code as a cold-stock row's unit carries it: 'D-39' for Savla D-39."""
    return next((c for c in grantable_codes() if same_site(c, store)), store)


def _cold_table(tables: dict) -> str:
    return tables["header"].replace("_rtv_header", "_cold_stocks")


def _dec(v) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def _same(a, b) -> bool:
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        return _dec(a) == _dec(b)
    return a == b


async def _exists(conn, table: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1::text) IS NOT NULL", table))


async def ims_rows(conn, tables: dict, cr_id: str) -> int:
    """How many cold-stock rows IMS made from this return's boxes (its marks:
    inward_transaction_no = the CR number, auto_created_from_inward), the rows the ERP
    made (cr_cold_stock_rows) left out. 0 when the company's cold table doesn't exist."""
    cold = _cold_table(tables)
    if not await _exists(conn, cold):
        return 0
    sql = (f"SELECT COUNT(*) FROM {cold} WHERE inward_transaction_no = $1 "
           "AND auto_created_from_inward IS TRUE")
    if not await _exists(conn, LINKS):
        return int(await conn.fetchval(sql, cr_id) or 0)
    return int(await conn.fetchval(
        sql + f" AND NOT EXISTS (SELECT 1 FROM {LINKS} k "
              f"WHERE k.cold_table = $2 AND k.cold_stock_id = {cold}.id)", cr_id, cold) or 0)


def _row_values(b: dict) -> dict:
    """What a box's cold-stock row copies from it (_COPIED), the line filling a blank."""
    return {"net_weight": _dec(b["net_weight"]), "lot_no": b["lot_no"], "item_mark": b["item_mark"],
            "vakkal": b["vakkal"], "spl_remarks": b["spl_remarks"]}


def _value(net: Decimal | None, rate) -> Decimal:
    return ((net or Decimal(0)) * (_dec(rate) or Decimal(0))).quantize(Decimal("0.01"), ROUND_HALF_UP)


async def _insert_row(conn, cold: str, cols: frozenset, cr_id: str, h: dict, store: str, b: dict,
                      v: dict) -> int:
    rtv_date = q._ist(h["rtv_date"])
    fields = [
        ("inward_dt", "::date", rtv_date.date() if isinstance(rtv_date, datetime) else None),
        ("unit", "", _unit(store)),
        ("inward_no", "", cr_id),
        ("item_description", "", b["item_description"]),
        ("item_mark", "", v["item_mark"]),
        ("vakkal", "", v["vakkal"]),
        ("lot_no", "", v["lot_no"]),
        ("no_of_cartons", "::numeric", Decimal(1)),
        ("weight_kg", "::numeric", v["net_weight"]),
        ("total_inventory_kgs", "::numeric", v["net_weight"]),
        ("group_name", "", b["item_category"]),
        ("item_subgroup", "", b["sub_category"]),
        ("storage_location", "", store),
        ("exporter", "", h["customer"]),
        ("last_purchase_rate", "::numeric", _dec(b["rate"])),
        ("value", "::numeric", _value(v["net_weight"], b["rate"])),
        ("box_id", "", b["box_id"]),
        ("transaction_no", "", cr_id),
        ("cold_item_mark", "", v["item_mark"]),
        ("spl_remarks", "", v["spl_remarks"]),
        ("inward_transaction_no", "", cr_id),
        ("auto_created_from_inward", "", True),
    ]
    fields = [f for f in fields if f[0] in cols]
    return await conn.fetchval(
        f"INSERT INTO {cold} ({', '.join(f[0] for f in fields)}) "
        f"VALUES ({', '.join(f'${i}{f[1]}' for i, f in enumerate(fields, 1))}) RETURNING id",
        *(f[2] for f in fields))


async def _correct_row(conn, cold: str, cols: frozenset, row_id: int, rate, was: dict, now: dict) -> bool:
    """Carry the box's changed values to its row; False when the row is gone."""
    sets: list[tuple[str, str, object]] = []
    if not _same(was["net_weight"], now["net_weight"]):
        sets += [("weight_kg", "::numeric", now["net_weight"]),
                 ("total_inventory_kgs", "::numeric", now["net_weight"]),
                 ("value", "::numeric", _value(now["net_weight"], rate))]
    if was["lot_no"] != now["lot_no"]:
        sets.append(("lot_no", "", now["lot_no"]))
    if was["item_mark"] != now["item_mark"]:
        sets += [("item_mark", "", now["item_mark"]), ("cold_item_mark", "", now["item_mark"])]
    if was["vakkal"] != now["vakkal"]:
        sets.append(("vakkal", "", now["vakkal"]))
    if was["spl_remarks"] != now["spl_remarks"]:
        sets.append(("spl_remarks", "", now["spl_remarks"]))
    sets = [s for s in sets if s[0] in cols]
    if not sets:
        return True
    done = await conn.execute(
        f"UPDATE {cold} SET {', '.join(f'{c} = ${i}{cast}' for i, (c, cast, _) in enumerate(sets, 2))} "
        "WHERE id = $1", row_id, *(s[2] for s in sets))
    return not str(done).endswith(" 0")


async def sync(conn, company: str, cr_id: str) -> dict:
    """Make or correct the cold-stock rows of an approved cold return's printed boxes
    (module docstring). Call it only for an approved return, inside the write's
    transaction; it takes the return's box lock itself. Returns {added, corrected}."""
    done = {"added": 0, "corrected": 0}
    tables = cr_table_names(company)
    h = await conn.fetchrow(
        f"SELECT id, rtv_date, factory_unit, customer FROM {tables['header']} WHERE rtv_id = $1", cr_id)
    store = cold_store(h["factory_unit"]) if h else None
    if store is None:
        return done
    cold = _cold_table(tables)
    if not await _exists(conn, LINKS):
        logger.warning("customer returns: %s is not in cold stock: %s is missing (migration 135)",
                       cr_id, LINKS)
        return done
    if not await _exists(conn, cold):
        logger.warning("customer returns: %s is not in cold stock: %s is missing", cr_id, cold)
        return done
    cols = frozenset(r["column_name"] for r in await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = $1", cold))
    if not _NEEDED <= cols:
        logger.warning("customer returns: %s is not in cold stock: %s has no %s",
                       cr_id, cold, ", ".join(sorted(_NEEDED - cols)))
        return done
    await q.lock_box_writes(conn, company, cr_id)
    if _IMS_MARKS <= cols and await ims_rows(conn, tables, cr_id):
        logger.info("customer returns: %s is in cold stock from IMS; left as it is", cr_id)
        return done

    key = (company or "").strip().upper()
    links = {r["box_id"]: dict(r) for r in await conn.fetch(
        f"SELECT id, box_id, cold_stock_id, {', '.join(_COPIED)} FROM {LINKS} "
        "WHERE company = $1 AND rtv_id = $2", key, cr_id)}
    boxes = await conn.fetch(
        f"""
        SELECT b.box_id, b.created_at, b.net_weight,
               COALESCE(b.lot_number, l.lot_number) AS lot_no,
               COALESCE(b.item_mark, l.item_mark) AS item_mark,
               COALESCE(b.vakkal, l.vakkal) AS vakkal,
               COALESCE(b.spl_remarks, l.spl_remarks) AS spl_remarks,
               l.item_description, l.item_category, l.sub_category, l.rate
          FROM {tables['boxes']} b
          JOIN LATERAL (
                SELECT l2.item_description, l2.item_category, l2.sub_category, l2.rate,
                       l2.lot_number, l2.item_mark, l2.vakkal, l2.spl_remarks
                  FROM {tables['lines']} l2
                 WHERE l2.header_id = b.header_id AND l2.item_description = b.article_description
                 ORDER BY l2.id LIMIT 1) l ON true
         WHERE b.header_id = $1 AND NULLIF(btrim(b.box_id), '') IS NOT NULL
         ORDER BY b.article_description, b.box_number, b.id
        """, h["id"])
    seen: set[str] = set()
    for b in map(dict, boxes):
        if b["box_id"] in seen:          # one label on two boxes: neither row may take the other's
            logger.warning("customer returns: %s has box id %s on two boxes; copied once",
                           cr_id, b["box_id"])
            continue
        seen.add(b["box_id"])
        now = _row_values(b)
        link = links.get(b["box_id"])
        if link is None:
            made = q._ist(b["created_at"])
            if not isinstance(made, datetime) or made < IMS_CLOSED:
                continue                       # written by IMS: IMS kept its cold stock
            row_id = await _insert_row(conn, cold, cols, cr_id, dict(h), store, b, now)
            await conn.execute(
                f"INSERT INTO {LINKS} (company, rtv_id, box_id, cold_table, cold_stock_id, "
                f"{', '.join(_COPIED)}) VALUES ($1, $2, $3, $4, $5, $6::numeric, $7, $8, $9, $10)",
                key, cr_id, b["box_id"], cold, row_id, *(now[c] for c in _COPIED))
            done["added"] += 1
            continue
        was = {c: link[c] for c in _COPIED}
        if all(_same(was[c], now[c]) for c in _COPIED):
            continue
        if not await _correct_row(conn, cold, cols, link["cold_stock_id"], b["rate"], was, now):
            logger.warning("customer returns: cold-stock row %s of %s box %s is gone; not made again",
                           link["cold_stock_id"], cr_id, b["box_id"])
            continue
        await conn.execute(
            f"UPDATE {LINKS} SET {', '.join(f'{c} = ${i}' for i, c in enumerate(_COPIED, 2))}, "
            "updated_at = NOW() WHERE id = $1", link["id"], *(now[c] for c in _COPIED))
        done["corrected"] += 1
    return done
