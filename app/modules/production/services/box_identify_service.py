"""Universal box identify: scan a QR, tell which table the box lives in.

Routing by QR structure (per the label formats in this ERP):
  * JSON  {"tx":"TR-...","bi":"91483060-1"}  -> warehouse / cold tables by
    (box_id, transaction_no).  bi = box id, tx = transaction number.
  * bare  "91483060-1"                        -> sfg_box by carton_id.
  * either primary misses                     -> exhaustive scan of every box
    table by box id alone.

The cfpl_*/cdpl_*/cold_transfer_inboxes tables are EXTERNALLY managed (no
CREATE TABLE in this repo). Rather than introspect each one per request, we run
the point-lookup directly and treat any DB-level failure (missing table/column
on those tables — cold_transfer_inboxes' schema is unknown here — or a
type/permission issue) as "box not in this table": one round-trip per table, no
schema queries. box id / txn columns are `box_id` / `transaction_no` for the
legacy family, `carton_id` / (none) for sfg_box.
"""
import json

import asyncpg

# (table, company, box_id_col, txn_col)  -- user-specified order for the JSON path.
_JSON_TABLES = [
    ("cfpl_bulk_entry_boxes", "cfpl", "box_id", "transaction_no"),
    ("cdpl_bulk_entry_boxes", "cdpl", "box_id", "transaction_no"),
    ("cfpl_boxes_v2",         "cfpl", "box_id", "transaction_no"),
    ("cdpl_boxes_v2",         "cdpl", "box_id", "transaction_no"),
    ("cold_transfer_inboxes", "",     "box_id", "transaction_no"),
    ("cfpl_cold_stocks",      "cfpl", "box_id", "transaction_no"),
    ("cdpl_cold_stocks",      "cdpl", "box_id", "transaction_no"),
]
_SFG = ("sfg_box", "", "carton_id", None)
# Everything scanned in the "search all tables" fallback (order = JSON set,
# then sfg_box, then po_box). Re-scanning the primary here is cheap and lets a
# mis-routed QR still resolve.
_ALL_TABLES = _JSON_TABLES + [_SFG, ("po_box", "", "box_id", "transaction_no")]

_ALIASES = {
    "box_id":          ("carton_id", "box_id"),
    "transaction_no":  ("transaction_no",),
    "item_description": ("item_description", "article_description", "fg_sku_name", "sfg_code"),
    "lot_number":      ("lot_number", "lot_no"),
    "net_weight":      ("net_weight", "weight_kg"),
    "gross_weight":    ("gross_weight",),
    "count":           ("count", "no_of_cartons", "units"),
    "status":          ("status",),
    "job_card_number": ("job_card_number",),
}


async def _query(conn, table, box_col, box_id, txn_col=None, txn=None):
    """First matching row as a dict, or None — one round-trip, no introspection.

    Table/column names come from the hardcoded allowlists above (never user
    input), so the f-string interpolation is injection-safe; only box_id/txn are
    parameterized. Any PostgresError (undefined table/column on the external
    tables, or a type/permission issue there) is swallowed as "not in this
    table"; our own bugs (TypeError/KeyError) still surface. Safe under the
    endpoint's autocommit — a failed statement is its own implicit txn and can't
    poison the connection. (Inside a conn.transaction() you'd need a SAVEPOINT.)
    """
    preds, args = [f"{box_col} = $1"], [box_id]
    if txn_col and txn is not None:
        preds.append(f"{txn_col} = $2")
        args.append(txn)
    try:
        row = await conn.fetchrow(
            f"SELECT * FROM {table} WHERE {' AND '.join(preds)} LIMIT 1", *args
        )
    except asyncpg.PostgresError:
        return None
    return dict(row) if row else None


def _pick(row, key):
    for col in _ALIASES[key]:
        v = row.get(col)
        if v not in (None, ""):
            return v
    return None


def _num(v):
    if v is None:
        return None
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return v


def _company(table, row):
    ent = row.get("entity")
    if ent:
        return ent
    if table.startswith("cfpl"):
        return "cfpl"
    if table.startswith("cdpl"):
        return "cdpl"
    return None


def _result(table, row, matched_by):
    return {
        "found": True,
        "table": table,
        "company": _company(table, row),
        "matched_by": matched_by,
        "box": {
            "box_id":          _pick(row, "box_id"),
            "transaction_no":  _pick(row, "transaction_no"),
            "item_description": _pick(row, "item_description"),
            "lot_number":      _pick(row, "lot_number"),
            "net_weight":      _num(_pick(row, "net_weight")),
            "gross_weight":    _num(_pick(row, "gross_weight")),
            "count":           _pick(row, "count"),
            "status":          _pick(row, "status"),
            "job_card_number": _pick(row, "job_card_number"),
        },
        # ponytail: no raw-row dump — cold_stocks carries cost cols
        # (last_purchase_rate/value) and this endpoint is unauthenticated.
    }


async def _search_all(conn, box_id, skip=()):
    for table, _company_hint, box_col, _txn_col in _ALL_TABLES:
        if table in skip:
            continue  # already tried with an identical box-id-only query
        row = await _query(conn, table, box_col, box_id)  # box id only
        if row:
            return _result(table, row, "box_id_only")
    return None


async def identify_box(conn, value: str) -> dict:
    value = (value or "").strip()
    tx = bi = None
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            tx, bi = parsed.get("tx"), parsed.get("bi")
    except (ValueError, TypeError):
        pass

    if tx and bi:
        bi, tx = str(bi).strip(), str(tx).strip()
        for table, _c, box_col, txn_col in _JSON_TABLES:
            row = await _query(conn, table, box_col, bi, txn_col, tx)
            if row:
                return _result(table, row, "tx+bi")
        return await _search_all(conn, bi) or {"found": False, "box_id": bi}

    # bare id, or a JSON QR that only carried `bi` -> resolve by that id.
    box_id = str(bi).strip() if bi else value
    if box_id:
        row = await _query(conn, _SFG[0], _SFG[2], box_id)
        if row:
            return _result(_SFG[0], row, "carton_id")
        # sfg_box already tried above with the same box-id-only query — skip it.
        found = await _search_all(conn, box_id, skip={_SFG[0]})
        if found:
            return found
    return {"found": False, "box_id": box_id}


if __name__ == "__main__":
    import asyncio

    class _FakeConn:
        """Rows keyed by (table, box_id). Tables in `broken` raise like a missing
        table/column would (they must be skipped, not fatal)."""
        def __init__(self, rows, broken=()):
            self.rows = rows
            self.broken = set(broken)
            self.seen = []  # table order actually queried

        async def fetchrow(self, sql, box_id, txn=None):
            table = sql.split("FROM ")[1].split(" WHERE")[0]
            self.seen.append(table)
            if table in self.broken:
                raise asyncpg.UndefinedColumnError("no such column")
            row = self.rows.get((table, box_id))
            if row is None:
                return None
            if txn is not None and row.get("transaction_no") != txn:
                return None
            return dict(row)

    async def _main():
        # 1. JSON QR resolves in a warehouse table by (box_id, transaction_no).
        c = _FakeConn({("cfpl_boxes_v2", "91-1"): {
            "box_id": "91-1", "transaction_no": "TR-9", "net_weight": 10.5}})
        r = await identify_box(c, '{"tx":"TR-9","bi":"91-1"}')
        assert r["found"] and r["table"] == "cfpl_boxes_v2" and r["matched_by"] == "tx+bi"
        assert r["box"]["net_weight"] == 10.5, r
        # bulk_entry tables are tried before boxes_v2 (user order)
        assert c.seen[:2] == ["cfpl_bulk_entry_boxes", "cdpl_bulk_entry_boxes"], c.seen

        # 2. Bare id resolves in sfg_box via carton_id.
        c = _FakeConn({("sfg_box", "48-2"): {
            "carton_id": "48-2", "fg_sku_name": "FG-X", "entity": "cdpl",
            "units": 5, "status": "PRINTED"}})
        r = await identify_box(c, "48-2")
        assert r["found"] and r["table"] == "sfg_box" and r["matched_by"] == "carton_id"
        assert r["box"]["box_id"] == "48-2" and r["company"] == "cdpl", r
        assert r["box"]["count"] == 5 and r["box"]["item_description"] == "FG-X", r

        # 3. JSON QR whose box is actually an sfg_box -> both primaries miss,
        #    exhaustive fallback finds it by box id alone.
        c = _FakeConn({("sfg_box", "77-3"): {"carton_id": "77-3", "entity": "cfpl"}})
        r = await identify_box(c, '{"tx":"TR-0","bi":"77-3"}')
        assert r["found"] and r["table"] == "sfg_box" and r["matched_by"] == "box_id_only", r

        # 4. Nothing anywhere -> found False, echoes the box id.
        c = _FakeConn({})
        r = await identify_box(c, "no-such")
        assert r == {"found": False, "box_id": "no-such"}, r

        # 4b. JSON with only `bi` (no tx) -> resolves by that id, not the raw JSON.
        c = _FakeConn({("sfg_box", "88-1"): {"carton_id": "88-1", "entity": "cfpl"}})
        r = await identify_box(c, '{"bi":"88-1"}')
        assert r["found"] and r["table"] == "sfg_box", r

        # 5. Wrong txn in JSON -> primary miss, fallback matches by box id only.
        c = _FakeConn({("cfpl_cold_stocks", "5-1"): {
            "box_id": "5-1", "transaction_no": "TR-REAL", "weight_kg": 3.2}})
        r = await identify_box(c, '{"tx":"TR-WRONG","bi":"5-1"}')
        assert r["found"] and r["matched_by"] == "box_id_only", r
        assert r["box"]["net_weight"] == 3.2, r  # weight_kg alias

        # 6. A table that errors (absent/unknown schema) is skipped, not fatal:
        #    the scan continues and still resolves in a later table.
        c = _FakeConn(
            {("cdpl_boxes_v2", "9-9"): {"box_id": "9-9", "transaction_no": "TR-1"}},
            broken={"cold_transfer_inboxes", "cfpl_boxes_v2"},
        )
        r = await identify_box(c, '{"tx":"TR-1","bi":"9-9"}')
        assert r["found"] and r["table"] == "cdpl_boxes_v2", r

        # 7. sfg_box isn't re-queried in the bare-path fallback (dedup).
        c = _FakeConn({("po_box", "7-7"): {"box_id": "7-7"}})
        r = await identify_box(c, "7-7")
        assert r["found"] and r["table"] == "po_box", r
        assert c.seen.count("sfg_box") == 1, c.seen  # primary only, not fallback

        print("box_identify self-check OK")

    asyncio.run(_main())
