"""Scan & send falling through to cold storage, and the dispatch that snapshots a
cold-only box from its cold table, against an in-memory fake database.

What is pinned:
  - lookup_service._search builds ONE query whose UNION ALL branches run
    boxes_v2 -> bulk_entry_boxes -> cold_stocks (each in the lookup's company
    order), the cold branches use _COLD_MATCH for the lookup's id column, and a
    table that does not exist is left out;
  - a cold row comes back as the box payload (box_number = numeric suffix of
    box_id, up to 9 digits, weight_kg as net and gross, group/subgroup as
    category/sub category), and an earlier table still wins over cold; a box
    still in transit on a transfer is refused (409) with its challan, from any
    site (where the box is: test_transfer_record_only pins it per path and table,
    test_transfer_box_rules every case of it);
  - reversal_service.park_boxes with cold_fallback=True (create/update) snapshots a
    cold-only box scanned at a non-cold From site (source_table, source_row_id,
    cold_storage_data), while from_storage_type stays the From site's; without it
    (backfill) the box parks with no source; the fallback matches box_id AND
    transaction_no only; a cold From site still reads cold first, the exact match
    in either company before a box_id-only one;
  - record only: no stock row is ever deleted, updated or inserted — a dispatch
    leaves the row it copied, and restore_to_source (cancel / edit) only drops the
    pending rows; stock_writes also flags a write to ERP Material In's tables
    (test_transfer_material_in reads them).

The fake keeps each table as a list of dict rows and EVALUATES the statements the
services send, for the small SQL subset they use (SELECT / [LEFT] JOIN / WHERE a = b,
a <> b, a IS NOT NULL, a = ANY($n::text[]) AND ... / LIMIT, UNION ALL ... ORDER BY
_prio, INSERT ... ON CONFLICT, UPDATE [... RETURNING], DELETE, ::casts, COALESCE,
NULLIF, btrim(x), substring(x from 'regex'), COUNT(*), CURRENT_TIMESTAMP), plus the box
guard's advisory-lock statement (recorded in `box_locks`). Anything else is an
AssertionError, so a change in the SQL's shape fails here instead of passing on a
stale stub. What Postgres would refuse is a PgError: a missing table or column, a
bad or out-of-range integer cast, text compared with a number. UNION ALL is
positional, as in Postgres: each branch's values are named by the first branch's
columns, and the branches must project the same names and cast types. The table
columns are the ones the services read and write (e.g. _PENDING_INSERT in
reversal_service); the real DDL lives outside this repo.

Run:  PYTHONPATH=. python -m pytest tests/services/test_transfer_box_lookup_cold.py
"""
from __future__ import annotations

import asyncio
import itertools
import json
import re
from datetime import date, datetime
from decimal import Decimal

import pytest
from asyncpg.exceptions import UniqueViolationError
from fastapi import HTTPException

from app.modules.transfer import schemas
from app.modules.transfer.services import create_service, lookup_service, query_service, reversal_service

TOUT = 51260001
CHALLAN = "TRANS202610151030"
USER = "store.a185@candorfoods.in"
DISPATCHED = datetime(2026, 10, 15)
BOX_ID, TNO = "34732254-5", "TR-2026-0042"


# ── A tiny SQL evaluator ─────────────────────────────────────────────────────
class PgError(Exception):
    """What the database itself would refuse."""


_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<param>\$\d+)|(?P<num>\d+(?:\.\d+)?)"
    r"|(?P<op>::|\[\]|<>|[(),=*])|(?P<name>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?))")
_KEYWORDS = {"WHERE", "LEFT", "JOIN", "LIMIT", "ORDER"}
_TYPES = {"int": "integer", "integer": "integer", "bigint": "bigint", "numeric": "numeric",
          "text": "text", "jsonb": "jsonb"}


def _tokenize(text: str) -> list[tuple[str, str]]:
    text, pos, out = text.strip(), 0, []
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        assert m and m.lastgroup, f"fake SQL cannot read: {text[pos:pos + 40]!r}"
        out.append((m.lastgroup, m.group(m.lastgroup)))
        pos = m.end()
    return out


class _Parser:
    def __init__(self, text: str):
        self.toks, self.i = _tokenize(text), 0

    # token helpers
    def peek(self, ahead=0):
        j = self.i + ahead
        return self.toks[j] if j < len(self.toks) else (None, None)

    def at(self, *words):
        return all(self.peek(k)[0] == "name" and self.peek(k)[1].upper() == w for k, w in enumerate(words))

    def word(self, *words):
        assert self.at(*words), f"expected {' '.join(words)} at: {self.rest()}"
        self.i += len(words)

    def is_op(self, v):
        return self.peek() == ("op", v)

    def op(self, v):
        assert self.is_op(v), f"expected {v!r} at: {self.rest()}"
        self.i += 1

    def name(self):
        kind, v = self.peek()
        assert kind == "name", f"expected a name at: {self.rest()}"
        self.i += 1
        return v

    def names(self):
        self.op("(")
        out = [self.name()]
        while self.is_op(","):
            self.i += 1
            out.append(self.name())
        self.op(")")
        return tuple(out)

    def rest(self):
        return " ".join(v for _, v in self.toks[self.i:self.i + 8])

    # expressions
    def expr(self):
        node = self.primary()
        while self.is_op("::"):
            self.i += 1
            typ = self.name().lower()
            if self.is_op("[]"):
                self.i += 1
                typ += "[]"
            node = ("cast", node, typ)
        return node

    def primary(self):
        kind, v = self.peek()
        self.i += 1
        if kind == "str":
            return ("lit", v[1:-1].replace("''", "'"))
        if kind == "num":
            return ("lit", Decimal(v) if "." in v else int(v))
        if kind == "param":
            return ("param", int(v[1:]))
        if (kind, v) == ("op", "("):
            node = self.expr()
            self.op(")")
            return node
        assert kind == "name", f"fake SQL cannot read {v!r} at: {self.rest()}"
        up = v.upper()
        if up == "NULL":
            return ("lit", None)
        if up in ("TRUE", "FALSE"):
            return ("lit", up == "TRUE")
        if up == "CURRENT_TIMESTAMP":
            return ("now",)
        if not self.is_op("("):
            qual, _, col = v.rpartition(".")
            return ("col", qual or None, col)
        self.i += 1
        if up == "SUBSTRING":
            s = self.expr()
            self.word("FROM")
            pattern = self.expr()
            self.op(")")
            return ("substring", s, pattern)
        if up == "CAST":
            e = self.expr()
            self.word("AS")
            typ = self.name().lower()
            self.op(")")
            return ("cast", e, typ)
        if up == "COUNT":
            self.op("*")
            self.op(")")
            return ("count",)
        if up == "ANY":
            arr = self.expr()
            self.op(")")
            return ("any", arr)
        assert up in ("COALESCE", "NULLIF", "BTRIM"), f"fake SQL has no function {v}"
        args = [self.expr()]
        while self.is_op(","):
            self.i += 1
            args.append(self.expr())
        self.op(")")
        return ("func", up, args)

    def conds(self):
        out = [self.cond()]
        while self.at("AND"):
            self.i += 1
            out.append(self.cond())
        return out

    def cond(self):
        left = self.expr()
        if self.at("IS", "NOT", "NULL"):
            self.i += 3
            return ("notnull", left, None)
        if self.is_op("<>"):
            self.i += 1
            return ("<>", left, self.expr())
        self.op("=")
        return ("=", left, self.expr())

    # statements
    def statement(self):
        if self.at("SELECT"):
            st = self.select()
        elif self.at("INSERT", "INTO"):
            st = self.insert()
        elif self.at("DELETE", "FROM"):
            self.i += 2
            st = {"op": "delete", "table": self.name(), "where": self.where()}
        elif self.at("UPDATE"):
            st = self.update()
        else:
            raise AssertionError(f"fake SQL has no statement: {self.rest()}")
        assert self.i == len(self.toks), f"fake SQL left unread: {self.rest()}"
        return st

    def where(self):
        if not self.at("WHERE"):
            return []
        self.i += 1
        return self.conds()

    def select(self):
        self.word("SELECT")
        items = []
        while True:
            if self.is_op("*"):
                self.i += 1
                items.append(("*", None))
            else:
                e = self.expr()
                if self.at("AS"):
                    self.i += 1
                    alias = self.name()
                else:
                    alias = e[2] if e[0] == "col" else e[0] if e[0] == "count" else "?column?"
                items.append((alias, e))
            if not self.is_op(","):
                break
            self.i += 1
        self.word("FROM")
        table = alias = self.name()
        if self.peek()[0] == "name" and self.peek()[1].upper() not in _KEYWORDS:
            alias = self.name()
        join = None
        if self.at("LEFT", "JOIN") or self.at("JOIN"):
            outer = self.at("LEFT")
            self.i += 2 if outer else 1
            jtable, jalias = self.name(), self.name()
            self.word("ON")
            join = (jtable, jalias, self.conds(), outer)
        where = self.where()
        limit = None
        if self.at("LIMIT"):
            self.i += 1
            limit = self.primary()[1]
        return {"op": "select", "items": items, "table": table, "alias": alias,
                "join": join, "where": where, "limit": limit}

    def insert(self):
        self.word("INSERT", "INTO")
        table = self.name()
        cols = self.names()
        self.word("VALUES")
        self.op("(")
        vals = [self.expr()]
        while self.is_op(","):
            self.i += 1
            vals.append(self.expr())
        self.op(")")
        assert len(cols) == len(vals), f"INSERT INTO {table}: {len(cols)} columns, {len(vals)} values"
        conflict = None
        if self.at("ON", "CONFLICT"):
            self.i += 2
            conflict = self.names() if self.is_op("(") else ()
            self.word("DO", "NOTHING")
        return {"op": "insert", "table": table, "cols": cols, "vals": vals,
                "conflict": conflict, "returning": self.returning()}

    def returning(self):
        if not self.at("RETURNING"):
            return None
        self.i += 1
        out = [self.name()]
        while self.is_op(","):
            self.i += 1
            out.append(self.name())
        return out

    def update(self):
        self.word("UPDATE")
        table = self.name()
        self.word("SET")
        sets = []
        while True:
            col = self.name()
            self.op("=")
            sets.append((col, self.expr()))
            if not self.is_op(","):
                break
            self.i += 1
        where = self.where()
        return {"op": "update", "table": table, "set": sets, "where": where, "returning": self.returning()}


def _cast(v, typ):
    if v is None:
        return None
    if typ.endswith("[]"):
        assert isinstance(v, (list, tuple)), f"pass a list for {typ}, not {type(v).__name__}"
        return [_cast(x, typ[:-2]) for x in v]
    if typ == "text":
        return v if isinstance(v, str) else str(v)
    if typ in ("bigint", "integer", "int"):
        if isinstance(v, str):
            if not re.fullmatch(r"\s*[+-]?\d+\s*", v):
                raise PgError(f'invalid input syntax for type {typ}: "{v}"')
            v = int(v)
        n = int(v)
        bits = 63 if typ == "bigint" else 31
        if not -(2 ** bits) <= n < 2 ** bits:
            raise PgError(f"value {v} out of range for type {typ}")
        return n
    if typ == "numeric":
        return v if isinstance(v, Decimal) else Decimal(str(v))
    if typ == "jsonb":
        return json.loads(v) if isinstance(v, str) else v
    raise AssertionError(f"fake SQL has no type {typ}")


def _eval(node, env, args):
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "param":
        return args[node[1] - 1]
    if kind == "col":
        _, qual, name = node
        if qual is None:
            assert len(env) == 1, f"ambiguous column {name}"
            (row,) = env.values()
        else:
            row = env[qual]
        if row is None:  # the NULL side of a LEFT JOIN
            return None
        if name not in row:
            raise PgError(f'column "{name}" does not exist')
        return row[name]
    if kind == "cast":
        return _cast(_eval(node[1], env, args), node[2])
    if kind == "now":
        return datetime.now()
    if kind == "substring":
        s, pattern = _eval(node[1], env, args), _eval(node[2], env, args)
        if s is None or pattern is None:
            return None
        assert "\\$" not in pattern
        # POSIX `$` is end-of-string; Python's `$` would also match before a final newline.
        m = re.search(pattern.replace("$", r"\Z"), s)
        if not m:
            return None
        return m.group(1) if m.re.groups else m.group(0)
    if kind == "func":
        vals = [_eval(a, env, args) for a in node[2]]
        if node[1] == "COALESCE":
            return next((v for v in vals if v is not None), None)
        if node[1] == "BTRIM":  # one argument: spaces off both ends, as Postgres trims
            (v,) = vals
            return None if v is None else v.strip(" ")
        return None if vals[0] == vals[1] else vals[0]  # NULLIF
    raise AssertionError(f"fake SQL cannot evaluate {node}")


def _eq(a, b):
    if a is None or b is None:
        return None
    num = (int, float, Decimal)
    if isinstance(a, bool) or isinstance(b, bool):
        if type(a) is not type(b):
            raise PgError(f"operator does not exist: {type(a).__name__} = {type(b).__name__}")
        return a == b
    if (isinstance(a, num) and isinstance(b, num)) or (isinstance(a, str) and isinstance(b, str)):
        return a == b
    raise PgError(f"operator does not exist: {type(a).__name__} = {type(b).__name__}")


def _holds(conds, env, args) -> bool:
    return all(_true(c, env, args) for c in conds)


def _true(cond, env, args) -> bool:
    """One WHERE / ON comparison, with SQL's NULL: anything compared with NULL is not true."""
    op, left, right = cond
    a = _eval(left, env, args)
    if op == "notnull":
        return a is not None
    if right[0] == "any":
        arr = _eval(right[1], env, args)
        return arr is not None and any(_eq(a, x) is True for x in arr)
    eq = _eq(a, _eval(right, env, args))
    return eq is False if op == "<>" else eq is True


def _decl_type(node):
    """The type a select item declares (its cast), for the UNION ALL column check."""
    if node is None:
        return None
    if node[0] == "cast":
        return _TYPES.get(node[2], node[2])
    if node[0] == "func":
        return next((t for t in map(_decl_type, node[2]) if t), None)
    if node[0] == "lit" and type(node[1]) is int:
        return "integer"
    return None


_UNION = re.compile(r"SELECT \* FROM \( (.+) \) u ORDER BY _prio LIMIT 1")
_BOX_LOCK = " ".join(create_service._LOCK_BOXES.split())


class _Tx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        self.db.tx_depth += 1
        return self

    async def __aexit__(self, *exc):
        self.db.tx_depth -= 1
        return False


class FakeDB:
    def __init__(self, **tables):
        base = {"pending_transfer_stock": [], "cold_stock_disposition": []}
        self.tables = {name: [dict(r) for r in rows] for name, rows in {**base, **tables}.items()}
        self.calls: list[tuple[str, str, tuple]] = []
        self.probes: list[str] = []      # to_regclass lookups, in order
        self.unions: list[list[tuple]] = []  # per UNION query: (table, _prio, WHERE) per branch
        self.tx_depth = 0
        self.box_locks: list[list[str]] = []  # create_service._LOCK_BOXES keys, per call
        self._serial = itertools.count(9_000_001)

    def transaction(self):
        return _Tx(self)

    def is_in_transaction(self):
        return bool(self.tx_depth)

    async def fetch(self, sql, *args):
        return self._run("fetch", sql, args)[0]

    async def fetchrow(self, sql, *args):
        rows = self._run("fetchrow", sql, args)[0]
        return rows[0] if rows else None

    async def fetchval(self, sql, *args):
        rows = self._run("fetchval", sql, args)[0]
        return next(iter(rows[0].values())) if rows else None

    async def execute(self, sql, *args):
        return self._run("execute", sql, args)[1]

    def statements(self, prefix: str) -> list[str]:
        return [s for _, s, _ in self.calls if s.startswith(prefix)]

    # internals
    def _table(self, name):
        if name not in self.tables:
            raise PgError(f'relation "{name}" does not exist')
        return self.tables[name]

    def _run(self, kind, sql, args):
        s = " ".join(sql.split())
        self.calls.append((kind, s, args))
        if s == "SELECT to_regclass($1)":
            name = args[0].removeprefix("public.")
            self.probes.append(name)
            return [{"to_regclass": args[0] if name in self.tables else None}], None
        if s == _BOX_LOCK:
            # The box guard's advisory locks: transaction-scoped, so only inside one.
            assert self.tx_depth, "advisory xact lock outside a transaction"
            self.box_locks.append(list(args[0]))
            return [], f"SELECT {len(args[0])}"
        m = _UNION.fullmatch(s)
        if m:
            return self._union(m.group(1), args), None
        if " FROM interunit_transfers_header h WHERE LOWER(" in s and "NOT EXISTS" in s:
            # backfill's candidate scan (status filter; no transfer-in rows exist here)
            keep = {"dispatch", "partial", "completed", "in transit"}
            cols = ("id", "challan_no", "from_site", "to_site", "status", "created_by", "created_ts")
            return [{c: r[c] for c in cols} for r in self._table("interunit_transfers_header")
                    if (r["status"] or "").lower() in keep], None
        st = _Parser(s).statement()
        return getattr(self, "_" + st["op"])(st, args)

    def _rows(self, st, args):
        rows = self._table(st["table"])
        join_rows = self._table(st["join"][0]) if st["join"] else None  # resolved up front, as Postgres does
        hits = []
        for row in rows:
            envs = [{st["alias"]: row}]
            if st["join"]:
                _, jalias, on, outer = st["join"]
                envs = [{st["alias"]: row, jalias: j} for j in join_rows
                        if _holds(on, {st["alias"]: row, jalias: j}, args)]
                if not envs and outer:
                    envs = [{st["alias"]: row, jalias: None}]
            hits += [env for env in envs if _holds(st["where"], env, args)]
        if st["items"] == [("count", ("count",))]:
            return [{"count": len(hits)}]
        out = []
        for env in hits:
            projected = {}
            for alias, e in st["items"]:
                if alias == "*":
                    (row,) = env.values()
                    projected.update(row)
                else:
                    projected[alias] = _eval(e, env, args)
            out.append(projected)
        return out if st["limit"] is None else out[:st["limit"]]

    def _select(self, st, args):
        return self._rows(st, args), None

    def _union(self, body, args):
        texts = body.split(" UNION ALL ")
        parsed = [_Parser(t).statement() for t in texts]
        names = [[a for a, _ in st["items"]] for st in parsed]
        for n in names:
            assert n == names[0], f"UNION ALL branches project different columns:\n{n}\n{names[0]}"
        for pos, col in enumerate(names[0]):
            types = {_decl_type(st["items"][pos][1]) for st in parsed} - {None}
            assert len(types) <= 1, f"UNION ALL column {col} is {types} across branches"
        self.unions.append([(st["table"], dict(st["items"])["_prio"][1], t.split(" WHERE ", 1)[1])
                            for st, t in zip(parsed, texts)])
        rows = [dict(zip(names[0], r.values())) for st in parsed for r in self._rows(st, args)]
        rows.sort(key=lambda r: r["_prio"])
        return rows[:1]

    def _insert(self, st, args):
        table = self._table(st["table"])
        row = {c: _eval(e, {}, args) for c, e in zip(st["cols"], st["vals"])}
        row.setdefault("id", next(self._serial))
        if any(r.get("id") == row["id"] for r in table):
            if st["conflict"] == ():
                return [], "INSERT 0 0"
            exc = UniqueViolationError(f'duplicate key value violates unique constraint "{st["table"]}_pkey"')
            exc.constraint_name = f"{st['table']}_pkey"
            raise exc
        if st["conflict"] and any(all(r.get(c) == row[c] for c in st["conflict"]) for r in table):
            return [], "INSERT 0 0"
        table.append(row)
        return ([{c: row[c] for c in st["returning"]}] if st["returning"] else []), "INSERT 0 1"

    def _update(self, st, args):
        out = []
        for row in self._table(st["table"]):
            env = {st["table"]: row}
            if _holds(st["where"], env, args):
                row.update({c: _eval(e, env, args) for c, e in st["set"]})
                out.append({c: _eval(("col", None, c), env, args) for c in st["returning"] or ()})
        return (out if st["returning"] else []), f"UPDATE {len(out)}"

    def _delete(self, st, args):
        table = self._table(st["table"])
        keep = [r for r in table if not _holds(st["where"], {st["table"]: r}, args)]
        n = len(table) - len(keep)
        table[:] = keep
        return [], f"DELETE {n}"


# Record only: the transfer module never writes a stock table (IMS keeps them), nor ERP
# Material In's tables and its inventory_batch copy (the purchase module keeps them).
STOCK_TABLES = tuple(f"{c}_{t}" for c in ("cfpl", "cdpl") for t in ("boxes_v2", "bulk_entry_boxes", "cold_stocks"))
MATERIAL_IN_TABLES = ("po_header", "po_line", "po_section", "po_box", "inventory_batch")
_STOCK_WRITE = re.compile(
    r"(?:INSERT INTO|UPDATE|DELETE FROM) (?:%s)\b" % "|".join((*STOCK_TABLES, *MATERIAL_IN_TABLES)))


def stock_writes(db: FakeDB) -> list[str]:
    """Every statement sent that wrote a stock table or a Material In table; record only
    means there are none."""
    return [s for _, s, _ in db.calls if _STOCK_WRITE.match(s)]


# ── Rows ─────────────────────────────────────────────────────────────────────
def _cold(**over):
    row = {
        "id": 7101, "inward_dt": date(2026, 9, 30), "unit": "Savla D-39", "inward_no": "INW-881",
        "item_description": "ALMOND 25KG", "item_mark": "RK", "vakkal": "V-12", "lot_no": "LOT-9",
        "no_of_cartons": Decimal("1"), "weight_kg": Decimal("24.750"),
        "total_inventory_kgs": Decimal("24.750"), "group_name": "NUTS", "item_subgroup": "ALMOND",
        "storage_location": "Savla D-39 Chamber 3", "exporter": "ACME AGRO",
        "last_purchase_rate": Decimal("812.50"), "value": Decimal("20109.38"),
        "box_id": BOX_ID, "transaction_no": TNO, "spl_remarks": None,
    }
    row.update(over)
    return row


def _v2(**over):
    row = {"id": 11, "box_id": BOX_ID, "transaction_no": TNO, "box_number": 5,
           "article_description": "ALMOND 25KG", "net_weight": Decimal("25.000"),
           "gross_weight": Decimal("25.400"), "lot_number": "", "batch_number": "B-1", "count": 1}
    row.update(over)
    return row


def _art(**over):
    row = {"id": 21, "transaction_no": TNO, "item_description": "ALMOND 25KG", "sku_id": 70012,
           "item_category": "NUTS", "sub_category": "ALMOND", "material_type": "RM",
           "lot_number": "LOT-9", "uom": "KG"}
    row.update(over)
    return row


def _bulk(**over):
    row = {"id": 31, "box_id": "88120417-3", "transaction_no": "BE-0007", "box_number": None,
           "article_description": "CASHEW W320", "net_weight": Decimal("10.000"),
           "gross_weight": Decimal("10.200"), "lot_number": "L-3"}
    row.update(over)
    return row


LOOKUP_TABLES = ("cfpl_boxes_v2", "cdpl_boxes_v2", "cfpl_articles_v2", "cdpl_articles_v2",
                 "cfpl_bulk_entry_boxes", "cdpl_bulk_entry_boxes", "cfpl_cold_stocks", "cdpl_cold_stocks")


def _db(*empty, **rows) -> FakeDB:
    return FakeDB(**{**{t: [] for t in empty}, **rows})


def _payload(**over) -> dict:
    """The box payload for _cold(), with `over` applied."""
    box = {
        "box_id": BOX_ID, "transaction_no": TNO, "box_number": 5,
        "article_description": "ALMOND 25KG", "item_description": "ALMOND 25KG", "sku_id": None,
        "item_category": "NUTS", "sub_category": "ALMOND", "material_type": None,
        "net_weight": 24.75, "gross_weight": 24.75, "lot_number": "LOT-9", "batch_number": "", "uom": None,
        "quantity_units": None, "packaging_type": None, "quality_grade": None, "count": None,
    }
    box.update(over)
    return box


V2_BOX = _payload(sku_id=70012, material_type="RM", net_weight=25.0, gross_weight=25.4,
                  batch_number="B-1", uom="KG")

# _cold_row_to_json(_cold()) as stored in pending_transfer_stock.cold_storage_data
SNAPSHOT = {
    "inward_dt": "2026-09-30", "unit": "Savla D-39", "inward_no": "INW-881", "item_mark": "RK",
    "vakkal": "V-12", "group_name": "NUTS", "item_subgroup": "ALMOND",
    "storage_location": "Savla D-39 Chamber 3", "exporter": "ACME AGRO",
    "last_purchase_rate": 812.5, "value": 20109.38, "total_inventory_kgs": 24.75, "spl_remarks": None,
}


@pytest.fixture(autouse=True)
def _fresh_existence_cache(monkeypatch):
    # lookup_service caches to_regclass answers for the process; each test starts clean.
    monkeypatch.setattr(lookup_service, "_EXISTS", {})


def _lookup(db, path, company="cfpl", *, box_id=BOX_ID, box_number=5, tno=TNO, from_wh=None) -> dict:
    if path == "manual":
        coro = lookup_service.get_box_by_number(db, company, box_number, tno, from_warehouse=from_wh)
    elif path == "tr":
        coro = lookup_service.get_box_by_box_id(db, company, box_id, tno, from_warehouse=from_wh)
    else:
        coro = lookup_service.get_bulk_entry_box(db, company, box_id, tno, from_warehouse=from_wh)
    out = asyncio.run(coro)
    assert out["success"] is True
    return out["box"]


def _ws(s: str) -> str:
    return " ".join(s.split())


# ── (1) the query: v2 -> bulk -> cold, one round trip ───────────────────────
COLD_WHERE = {
    "box_id": "box_id = $1 AND transaction_no = $2",
    "box_number": "transaction_no = $2 AND substring(box_id from '-([0-9]{1,9})$')::bigint = $1",
}


@pytest.mark.parametrize("path, company, order, id_col, id_val", [
    ("manual", "cfpl", ["cfpl_boxes_v2", "cdpl_boxes_v2", "cfpl_cold_stocks", "cdpl_cold_stocks"],
     "box_number", 5),
    ("manual", "cdpl", ["cdpl_boxes_v2", "cfpl_boxes_v2", "cdpl_cold_stocks", "cfpl_cold_stocks"],
     "box_number", 5),
    ("tr", "cdpl", ["cfpl_boxes_v2", "cdpl_boxes_v2", "cfpl_bulk_entry_boxes", "cdpl_bulk_entry_boxes",
                    "cfpl_cold_stocks", "cdpl_cold_stocks"], "box_id", BOX_ID),
    ("be", "cdpl", ["cfpl_bulk_entry_boxes", "cdpl_bulk_entry_boxes", "cfpl_cold_stocks", "cdpl_cold_stocks"],
     "box_id", BOX_ID),
], ids=["manual-cfpl", "manual-cdpl", "tr-qr", "be-qr"])
def test_each_lookup_reads_v2_then_bulk_then_cold_in_one_query(path, company, order, id_col, id_val):
    db = _db(*LOOKUP_TABLES)
    with pytest.raises(HTTPException) as exc:
        _lookup(db, path, company)
    assert exc.value.status_code == 404

    queries = [c for c in db.calls if c[1] != "SELECT to_regclass($1)"]
    assert [(kind, args) for kind, _, args in queries] == [("fetchrow", (id_val, TNO))]
    (branches,) = db.unions
    assert [t for t, _, _ in branches] == order
    assert [p for _, p, _ in branches] == list(range(1, len(order) + 1))
    for table, _, where in branches:
        if table.endswith("_cold_stocks"):
            assert where == _ws(COLD_WHERE[id_col]) == _ws(lookup_service._COLD_MATCH[id_col])
        elif table.endswith("_boxes_v2"):
            assert where == f"b.{id_col} = $1 AND b.transaction_no = $2"
        else:
            assert where == "box_id = $1 AND transaction_no = $2"


def test_tables_that_do_not_exist_are_left_out_and_probed_once():
    db = _db("cfpl_boxes_v2", "cfpl_articles_v2", cdpl_cold_stocks=[_cold()])
    assert _lookup(db, "tr") == _payload()
    # ...and, for the hit, interunit_transfer_boxes (where the box is; without it, no check).
    assert db.probes == ["cfpl_boxes_v2", "cdpl_boxes_v2", "cfpl_bulk_entry_boxes",
                         "cdpl_bulk_entry_boxes", "cfpl_cold_stocks", "cdpl_cold_stocks",
                         "interunit_transfer_boxes"]
    assert [(t, p) for t, p, _ in db.unions[0]] == [("cfpl_boxes_v2", 1), ("cdpl_cold_stocks", 2)]

    # The answers are cached: the next scan sends its query without probing again.
    assert _lookup(db, "be") == _payload()
    assert len(db.probes) == 7
    assert [(t, p) for t, p, _ in db.unions[1]] == [("cdpl_cold_stocks", 1)]


@pytest.mark.parametrize("path", ["manual", "tr", "be"])
def test_with_none_of_the_tables_a_lookup_is_404_without_a_query(path):
    db = _db()
    with pytest.raises(HTTPException) as exc:
        _lookup(db, path)
    assert exc.value.status_code == 404
    assert {s for _, s, _ in db.calls} == {"SELECT to_regclass($1)"}


# ── (2) the cold row as a box ──────────────────────────────────────────────
@pytest.mark.parametrize("path, row, expected", [
    ("manual", _cold(), _payload()),
    ("tr", _cold(), _payload()),
    ("be", _cold(box_id="88120417-3", transaction_no="BE-0007"),
     _payload(box_id="88120417-3", transaction_no="BE-0007", box_number=3)),
])
def test_a_box_held_only_in_cold_storage_is_found_on_every_path(path, row, expected):
    db = _db(*LOOKUP_TABLES, cdpl_cold_stocks=[_cold(id=1, box_id="OTHER-1"), row])
    got = _lookup(db, path, box_id=row["box_id"], box_number=expected["box_number"], tno=row["transaction_no"])
    assert got == expected
    # Cost columns (last_purchase_rate / value) never reach the scanner.
    assert "value" not in got and "last_purchase_rate" not in got


def test_a_cold_row_without_group_subgroup_or_weight_maps_to_empty_and_zero():
    db = _db(*LOOKUP_TABLES, cfpl_cold_stocks=[_cold(group_name=None, item_subgroup=None, weight_kg=None,
                                                    lot_no=None)])
    assert _lookup(db, "tr") == _payload(item_category="", sub_category="", net_weight=0.0, gross_weight=0.0,
                                         lot_number=None)


@pytest.mark.parametrize("box_id, number", [
    ("34732254-5", 5),
    ("34732254-007", 7),
    ("TR-1-12", 12),
    ("NOSUFFIX", 0),
    ("34732254-5A", 0),
    ("34732254-", 0),
    ("X-" + "9" * 9, 999_999_999),
    ("X-2147483648", 0),    # past 9 digits: left at 0 (an integer column / JS number holds it), never cast
    ("X-" + "9" * 19, 0),
    ("X-" + "1" * 24, 0),
])
def test_cold_box_number_is_the_numeric_suffix_of_box_id(box_id, number):
    db = _db(*LOOKUP_TABLES, cfpl_cold_stocks=[_cold(box_id=box_id)])
    assert _lookup(db, "tr", box_id=box_id)["box_number"] == number


def test_manual_entry_finds_a_cold_box_by_transaction_and_box_id_suffix():
    db = _db(*LOOKUP_TABLES, cfpl_cold_stocks=[
        _cold(id=1),
        _cold(id=2, box_id="34732254-6", item_description="ALMOND 10KG"),
        _cold(id=3, transaction_no="TR-2026-0099", item_description="CASHEW W240"),
        _cold(id=4, box_id="LEGACY-" + "7" * 24),   # a suffix past bigint must not break the query
        _cold(id=5, box_id="NOSUFFIX"),
    ])
    assert _lookup(db, "manual", box_number=5) == _payload()
    assert _lookup(db, "manual", box_number=6)["item_description"] == "ALMOND 10KG"
    assert _lookup(db, "manual", box_number=5, tno="TR-2026-0099")["item_description"] == "CASHEW W240"
    for number, tno in ((7, TNO), (5, "TR-2026-0001"), (0, TNO)):
        with pytest.raises(HTTPException) as exc:
            _lookup(db, "manual", box_number=number, tno=tno)
        assert exc.value.status_code == 404


@pytest.mark.parametrize("company, first", [("cfpl", "CFPL"), ("cdpl", "CDPL"), ("CDPL", "CDPL"), ("", "CFPL")])
def test_manual_entry_reads_the_named_company_cold_stock_first(company, first):
    db = _db(*LOOKUP_TABLES, cfpl_cold_stocks=[_cold(item_description="CFPL")],
             cdpl_cold_stocks=[_cold(item_description="CDPL")])
    assert _lookup(db, "manual", company)["item_description"] == first
    # The QR paths are company-agnostic: cfpl first.
    assert _lookup(db, "tr", company)["item_description"] == "CFPL"


@pytest.mark.parametrize("path", ["manual", "tr"])
def test_a_box_in_boxes_v2_and_cold_stocks_returns_the_boxes_v2_row(path):
    # A cold-destined inward writes the same box into both; v2 wins even from the other company.
    db = _db(*LOOKUP_TABLES, cdpl_boxes_v2=[_v2()], cdpl_articles_v2=[_art()], cfpl_cold_stocks=[_cold()])
    assert _lookup(db, path, "cfpl") == V2_BOX


def test_a_bulk_box_also_in_cold_stocks_returns_the_bulk_row():
    db = _db(*LOOKUP_TABLES, cdpl_bulk_entry_boxes=[_bulk()],
             cfpl_cold_stocks=[_cold(box_id="88120417-3", transaction_no="BE-0007")])
    assert _lookup(db, "be", box_id="88120417-3", tno="BE-0007") == _payload(
        box_id="88120417-3", transaction_no="BE-0007", box_number=0, article_description="CASHEW W320",
        item_description="CASHEW W320", item_category="", sub_category="", material_type="RM",
        net_weight=10.0, gross_weight=10.2, lot_number="L-3", uom="BAG")


def test_a_missing_box_is_404_and_the_bulk_message_names_cold_storage():
    db = _db(*LOOKUP_TABLES, cfpl_cold_stocks=[_cold(box_id="OTHER-5")])
    with pytest.raises(HTTPException) as exc:
        _lookup(db, "be")
    assert exc.value.status_code == 404
    assert exc.value.detail == (f"Box with box_id '{BOX_ID}' and transaction_no '{TNO}' "
                                "not found in bulk entry boxes or cold storage")
    with pytest.raises(HTTPException) as exc:
        _lookup(db, "manual", box_number=6)
    assert exc.value.status_code == 404 and exc.value.detail == f"Box #6 with transaction_no '{TNO}' not found"


# ── (3)-(6) parking the dispatched box ──────────────────────────────────────
WAREHOUSE_TABLES = ("cfpl_boxes_v2", "cdpl_boxes_v2", "cfpl_bulk_entry_boxes", "cdpl_bulk_entry_boxes",
                    "cfpl_cold_stocks", "cdpl_cold_stocks")
NO_STATS = {"parked_from_cold": 0, "parked_from_warehouse": 0, "parked_without_source": 0,
            "skipped_already_parked": 0, "missing_id": 0}


def _box(**over):
    """A dispatched box as create_service hands it to park_boxes. Its article / lot /
    weight differ from the cold row's, so the tests show which one a field came from."""
    b = {"box_id": BOX_ID, "transaction_no": TNO, "article": "Almond 25 kg", "lot_number": "LOT-X",
         "batch_number": "", "net_weight": "25", "gross_weight": "25.5"}
    b.update(over)
    return b


def _park(db, boxes, *, from_site="A185", to_site="W202", cold_fallback=True) -> dict:
    async def go():
        async with db.transaction():
            return await reversal_service.park_boxes(
                db, transfer_out_id=TOUT, challan_no=CHALLAN, from_site=from_site, to_site=to_site,
                boxes=boxes, dispatched_at=DISPATCHED, dispatched_by=USER, cold_fallback=cold_fallback)
    return asyncio.run(go())


def _pending(db) -> dict:
    (p,) = db.tables["pending_transfer_stock"]
    return {k: v for k, v in p.items() if k != "id"}


def _parked(**over) -> dict:
    p = {
        "transfer_type": "INTERUNIT", "transfer_out_id": TOUT, "transfer_out_challan_no": CHALLAN,
        "box_id": BOX_ID, "transaction_no": TNO, "from_company": "cdpl", "to_company": "cdpl",
        "from_site": "A185", "to_site": "W202", "from_storage_type": "warehouse", "to_storage_type": "warehouse",
        "source_table": "cdpl_cold_stocks", "source_row_id": 7101, "destination_table": "cdpl_boxes_v2",
        "item_description": "ALMOND 25KG", "lot_no": "LOT-9", "weight_kg": Decimal("24.75"),
        "no_of_cartons": 1, "cold_storage_data": SNAPSHOT,
        "gross_weight": Decimal("25.5"), "net_weight": Decimal("25"), "article": "Almond 25 kg",
        "status": "In Transit", "dispatched_at": DISPATCHED, "dispatched_by": USER,
    }
    p.update(over)
    return p


@pytest.mark.parametrize("to_site, to_st, dest", [("W202", "warehouse", "cdpl_boxes_v2"),
                                                  ("Rishi", "cold", "cdpl_cold_stocks")])
def test_a_warehouse_dispatch_snapshots_a_cold_only_box_and_leaves_its_cold_row(to_site, to_st, dest):
    other = _cold(id=7102, box_id="34732254-6")
    db = _db(*WAREHOUSE_TABLES, cdpl_cold_stocks=[_cold(), other])
    stats = _park(db, [_box()], to_site=to_site)

    assert stats == {**NO_STATS, "parked_from_cold": 1}
    # Record only: the cold row is copied (source_row_id 7101, the snapshot), not taken out.
    assert db.tables["cdpl_cold_stocks"] == [_cold(), other]
    assert not db.statements("DELETE") and not db.statements("UPDATE") and stock_writes(db) == []
    assert _pending(db) == _parked(to_site=to_site, to_storage_type=to_st, destination_table=dest)


def test_without_cold_fallback_the_box_parks_without_source_and_nothing_is_deleted():
    db = _db(*WAREHOUSE_TABLES, cdpl_cold_stocks=[_cold()])
    stats = _park(db, [_box()], cold_fallback=False)

    assert stats == {**NO_STATS, "parked_without_source": 1}
    assert db.tables["cdpl_cold_stocks"] == [_cold()]
    assert not db.statements("DELETE")
    assert not [s for s in db.statements("SELECT *") if "_cold_stocks" in s]
    assert _pending(db) == _parked(
        from_company="cfpl", to_company="cfpl", from_storage_type="warehouse",
        source_table="cfpl_bulk_entry_boxes", source_row_id=None, destination_table="cfpl_boxes_v2",
        item_description="Almond 25 kg", lot_no="LOT-X", weight_kg=Decimal("25.0"), cold_storage_data=None)


def test_backfill_keeps_its_old_behaviour_for_a_cold_only_box():
    header = {"id": TOUT, "challan_no": CHALLAN, "from_site": "A185", "to_site": "W202", "status": "Dispatch",
              "created_by": USER, "created_ts": DISPATCHED}
    box = {"id": 1, "header_id": TOUT, "box_id": BOX_ID, "transaction_no": TNO, "article": "ALMOND 25KG",
           "lot_number": "LOT-9", "batch_number": "", "net_weight": Decimal("24.750"),
           "gross_weight": Decimal("24.750")}
    db = _db(*WAREHOUSE_TABLES, cdpl_cold_stocks=[_cold()], interunit_transfers_header=[header],
             interunit_transfer_boxes=[box])

    s = asyncio.run(reversal_service.backfill_pending_from_existing_transfers(db))

    assert s == {"transfers_scanned": 1, "transfers_with_existing_pending": 0, "boxes_parked_from_cold": 0,
                 "boxes_parked_from_warehouse": 0, "boxes_parked_without_source": 1,
                 "boxes_skipped_already_parked": 0, "boxes_with_missing_id": 0,
                 "boxes_skipped_sent_on": 0, "boxes_skipped_received": 0}
    assert db.tables["cdpl_cold_stocks"] == [_cold()] and not db.statements("DELETE")
    p = _pending(db)
    assert (p["source_table"], p["source_row_id"], p["from_storage_type"], p["cold_storage_data"]) == (
        "cfpl_bulk_entry_boxes", None, "warehouse", None)


def test_a_cold_row_with_the_box_id_but_another_transaction_is_not_taken():
    stale = _cold(transaction_no="TR-2026-0001")   # same label, another inward
    db = _db(*WAREHOUSE_TABLES, cfpl_cold_stocks=[stale])
    stats = _park(db, [_box()])

    assert stats == {**NO_STATS, "parked_without_source": 1}
    assert db.tables["cfpl_cold_stocks"] == [stale] and not db.statements("DELETE")
    # Exact (box_id, transaction_no) reads only; never box_id alone.
    assert [s for s in db.statements("SELECT *") if "_cold_stocks" in s] == [
        f"SELECT * FROM {t} WHERE box_id = $1 AND transaction_no = $2 LIMIT 1"
        for t in ("cfpl_cold_stocks", "cdpl_cold_stocks")]
    p = _pending(db)
    assert (p["source_table"], p["from_storage_type"], p["cold_storage_data"]) == (
        "cfpl_bulk_entry_boxes", "warehouse", None)
    # ...which is what the scan offered: it does not match that row either.
    with pytest.raises(HTTPException):
        _lookup(_db(*LOOKUP_TABLES, cfpl_cold_stocks=[stale]), "tr")


def test_a_warehouse_dispatch_of_a_box_in_boxes_v2_and_cold_snapshots_the_boxes_v2_row():
    # The lookup shows the v2 row for such a box, so that is the row copied.
    db = _db(*WAREHOUSE_TABLES, cfpl_boxes_v2=[_v2()], cfpl_cold_stocks=[_cold()])
    stats = _park(db, [_box()])

    assert stats == {**NO_STATS, "parked_from_warehouse": 1}
    assert db.tables["cfpl_boxes_v2"] == [_v2()] and db.tables["cfpl_cold_stocks"] == [_cold()]
    assert not [s for s in db.statements("SELECT *") if "_cold_stocks" in s]
    p = _pending(db)
    assert (p["source_table"], p["source_row_id"], p["from_storage_type"], p["cold_storage_data"]) == (
        "cfpl_boxes_v2", 11, "warehouse", None)


@pytest.mark.parametrize("path", ["manual", "tr"])
@pytest.mark.parametrize("from_wh", [None, "A185", "W202"])
def test_a_box_on_a_transfer_is_refused_on_a_rescan_with_its_challan(path, from_wh, no_readback):
    # A dispatch copies the boxes_v2 row and leaves it (and its cold twin) where they are,
    # so a re-scan still finds the box; while it is in transit it must not go onto a
    # second challan, from whichever site.
    db = _db(*LOOKUP_TABLES, "interunit_transfers_header", "interunit_transfers_lines",
             "interunit_transfer_boxes", cfpl_boxes_v2=[_v2()], cfpl_articles_v2=[_art()],
             cfpl_cold_stocks=[_cold()])
    asyncio.run(create_service.create_transfer(db, _transfer(), USER))
    (header,) = db.tables["interunit_transfers_header"]
    assert db.tables["cfpl_boxes_v2"] == [_v2()] and db.tables["cfpl_cold_stocks"] == [_cold()]

    with pytest.raises(HTTPException) as exc:
        _lookup(db, path, from_wh=from_wh)
    assert exc.value.status_code == 409
    assert exc.value.detail == f"Box {BOX_ID} is already on challan {header['challan_no']} (in transit)"


def test_a_mixed_dispatch_parks_each_box_from_where_it_was_found():
    db = _db(*WAREHOUSE_TABLES, cfpl_bulk_entry_boxes=[_bulk()], cdpl_cold_stocks=[_cold()])
    stats = _park(db, [_box(box_id="88120417-3", transaction_no="BE-0007"), _box()])

    assert stats == {**NO_STATS, "parked_from_cold": 1, "parked_from_warehouse": 1}
    assert db.tables["cfpl_bulk_entry_boxes"] == [_bulk()] and db.tables["cdpl_cold_stocks"] == [_cold()]
    assert {p["box_id"]: (p["source_table"], p["cold_storage_data"])
            for p in db.tables["pending_transfer_stock"]} == {
        "88120417-3": ("cfpl_bulk_entry_boxes", None),
        BOX_ID: ("cdpl_cold_stocks", SNAPSHOT),
    }
    # One From site, one storage type: the in-transit list (grouped on it) shows one challan.
    assert {p["from_storage_type"] for p in db.tables["pending_transfer_stock"]} == {"warehouse"}


def test_a_box_already_in_transit_is_skipped_and_its_cold_row_kept():
    db = _db(*WAREHOUSE_TABLES, cdpl_cold_stocks=[_cold()],
             pending_transfer_stock=[{"id": 1, "box_id": BOX_ID, "transaction_no": TNO, "transfer_out_id": 1}])
    assert _park(db, [_box()]) == {**NO_STATS, "skipped_already_parked": 1}
    assert db.tables["cdpl_cold_stocks"] == [_cold()] and len(db.tables["pending_transfer_stock"]) == 1


@pytest.mark.parametrize("cold_fallback", [True, False])
def test_a_cold_from_site_snapshots_the_cold_twin_before_boxes_v2(cold_fallback):
    db = _db(*WAREHOUSE_TABLES, cfpl_boxes_v2=[_v2()], cfpl_cold_stocks=[_cold()])
    stats = _park(db, [_box()], from_site="Rishi", cold_fallback=cold_fallback)

    assert stats == {**NO_STATS, "parked_from_cold": 1}
    assert db.tables["cfpl_cold_stocks"] == [_cold()] and db.tables["cfpl_boxes_v2"] == [_v2()]
    assert _pending(db) == _parked(from_company="cfpl", to_company="cfpl", from_site="Rishi",
                                   from_storage_type="cold",
                                   source_table="cfpl_cold_stocks", destination_table="cfpl_boxes_v2")


@pytest.mark.parametrize("cold_fallback", [True, False])
def test_a_cold_from_site_still_matches_a_cold_row_by_box_id_alone(cold_fallback):
    db = _db(*WAREHOUSE_TABLES, cfpl_cold_stocks=[_cold(transaction_no="TR-2026-0001")])
    stats = _park(db, [_box()], from_site="Savla D-39", cold_fallback=cold_fallback)

    assert stats == {**NO_STATS, "parked_from_cold": 1}
    assert db.tables["cfpl_cold_stocks"] == [_cold(transaction_no="TR-2026-0001")]
    p = _pending(db)
    assert (p["transaction_no"], p["source_table"], p["from_storage_type"]) == (TNO, "cfpl_cold_stocks", "cold")


def test_a_cold_from_site_snapshots_the_exact_cdpl_row_before_a_cfpl_row_sharing_the_box_id():
    # box_id alone is not unique: the cfpl row is another box that happens to share the label.
    namesake = _cold(id=7201, transaction_no="TR-2026-0001", item_description="CASHEW W240")
    db = _db(*WAREHOUSE_TABLES, cfpl_cold_stocks=[namesake], cdpl_cold_stocks=[_cold()])
    stats = _park(db, [_box()], from_site="Savla D-39")

    assert stats == {**NO_STATS, "parked_from_cold": 1}
    assert db.tables["cfpl_cold_stocks"] == [namesake] and db.tables["cdpl_cold_stocks"] == [_cold()]
    p = _pending(db)
    assert (p["source_table"], p["source_row_id"], p["item_description"]) == (
        "cdpl_cold_stocks", 7101, "ALMOND 25KG")


@pytest.mark.parametrize("cold_fallback", [True, False])
def test_a_cold_from_site_falls_back_to_the_warehouse_tables(cold_fallback):
    db = _db(*WAREHOUSE_TABLES, cfpl_boxes_v2=[_v2()])
    stats = _park(db, [_box()], from_site="Rishi", cold_fallback=cold_fallback)

    assert stats == {**NO_STATS, "parked_from_warehouse": 1}
    assert db.tables["cfpl_boxes_v2"] == [_v2()]
    p = _pending(db)
    assert (p["source_table"], p["from_storage_type"], p["cold_storage_data"]) == ("cfpl_boxes_v2", "cold", None)


@pytest.mark.parametrize("from_site", ["A185", "Rishi"])
def test_a_cancel_drops_the_pending_row_and_leaves_the_cold_row_as_it_was(from_site):
    # Nothing was taken out of cold, so nothing is put back: no unit / location is made up
    # for the row either (it had none), whatever the From site.
    row = _cold(unit=None, storage_location=None)
    db = _db(*WAREHOUSE_TABLES, cfpl_cold_stocks=[row],
             pending_transfer_stock=[{"id": 1, "box_id": "LINE-9-1", "transaction_no": CHALLAN,
                                      "transfer_out_id": TOUT},
                                     {"id": 2, "box_id": "OTHER-1", "transaction_no": TNO,
                                      "transfer_out_id": TOUT + 1}])
    assert _park(db, [_box()], from_site=from_site) == {**NO_STATS, "parked_from_cold": 1}

    # This transfer's box and its LINE- row go; another transfer's row stays.
    assert asyncio.run(reversal_service.restore_to_source(db, TOUT)) == 2
    assert [r["box_id"] for r in db.tables["pending_transfer_stock"]] == ["OTHER-1"]
    assert db.tables["cfpl_cold_stocks"] == [row] and stock_writes(db) == []


# ── create / update go through the fallback; a cancel only drops the pending rows ────
@pytest.fixture
def no_readback(monkeypatch):
    async def get_transfer(conn, transfer_id):
        return {"id": transfer_id}
    monkeypatch.setattr(query_service, "get_transfer", get_transfer)


def _transfer(*, with_box=True) -> schemas.TransferCreate:
    return schemas.TransferCreate(
        header=schemas.TransferHeaderCreate(stock_trf_date="15-10-2026", from_warehouse="A185",
                                            to_warehouse="W202", vehicle_no="MH43BP6885"),
        lines=[schemas.TransferLineCreate(material_type="RM", item_category="NUTS", sub_category="ALMOND",
                                          item_description="ALMOND 25KG", quantity="1", pack_size="24.75",
                                          lot_number="LOT-9")],
        boxes=[schemas.BoxCreate(box_number=5, box_id=BOX_ID, article="ALMOND 25KG", lot_number="LOT-9",
                                 transaction_no=TNO, net_weight="24.75", gross_weight="24.75")]
        if with_box else None,
    )


def _transfer_db() -> FakeDB:
    return _db("interunit_transfers_header", "interunit_transfers_lines", "interunit_transfer_boxes",
               cfpl_cold_stocks=[_cold()])


def test_create_transfer_snapshots_a_cold_only_box_scanned_at_a_warehouse(no_readback):
    db = _transfer_db()
    out = asyncio.run(create_service.create_transfer(db, _transfer(), USER))

    (header,) = db.tables["interunit_transfers_header"]
    assert out == {"id": header["id"]}
    assert db.tables["cfpl_cold_stocks"] == [_cold()]
    p = _pending(db)
    assert (p["source_table"], p["source_row_id"], p["from_storage_type"], p["cold_storage_data"]) == (
        "cfpl_cold_stocks", 7101, "warehouse", SNAPSHOT)
    assert (header["status"], header["unallocated_boxes"]) == ("Dispatch", 0)


def test_editing_the_transfer_reparks_the_cold_box_and_never_touches_cold(no_readback):
    # An edit locks the transfer's header row (FOR UPDATE), which DispatchDB reads.
    from tests.services.test_transfer_dispatch_lock import DispatchDB
    db = DispatchDB(**_transfer_db().tables)
    asyncio.run(create_service.create_transfer(db, _transfer(), USER))
    (header,) = db.tables["interunit_transfers_header"]

    # Same boxes (judged without this transfer, so free): the pending row is dropped and
    # parked again from the same cold row, which stays as it is.
    asyncio.run(create_service.update_transfer(db, header["id"], _transfer()))
    assert db.tables["cfpl_cold_stocks"] == [_cold()]
    p = _pending(db)
    assert (p["source_table"], p["source_row_id"], p["from_storage_type"], p["cold_storage_data"]) == (
        "cfpl_cold_stocks", 7101, "warehouse", SNAPSHOT)

    # Box edited out: its pending row goes; the cold row is still the same row (same id).
    asyncio.run(create_service.update_transfer(db, header["id"], _transfer(with_box=False)))
    assert db.tables["cfpl_cold_stocks"] == [_cold()]
    assert all(r["box_id"].startswith("LINE-") and r["source_table"] == ""
               for r in db.tables["pending_transfer_stock"])
    # Transfer records only (no boxes_v2 / bulk table exists here, so a write there would raise).
    assert {s.split()[2] for s in db.statements("INSERT INTO")} == {
        "interunit_transfers_header", "interunit_transfers_lines", "interunit_transfer_boxes",
        "pending_transfer_stock"}
    assert stock_writes(db) == []
