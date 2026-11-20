"""SO Creation's second upload: the Tally Sales Register (invoices raised),
its save to sales_invoice / sales_invoice_line, and the Pending Orders export.

A small workbook in the 19 Nov 2026 export's layout is built here (never the
real file): preamble, header row, a sales voucher with two items, a cancelled
voucher, a service voucher with no items, a replacement invoice, Grand Total.
"""
import asyncio
import io
import json
from datetime import date, datetime

import openpyxl
import pytest

from app.modules.so.schemas.sales_invoice import SalesInvoiceUploadResponse
from app.modules.so.services import pending_orders
from app.modules.so.services.item_matcher import MasterItem
from app.modules.so.services.sales_invoice_parser import (
    dispatch_site,
    parse_sales_invoices,
    so_number_of,
)
from app.modules.so.services.sales_invoices import (
    InvoiceTablesMissing,
    process_sales_invoices,
    save_sales_invoices,
)

HEADER = ["Date", "Particulars", "Buyer", "Buyer Address", "Consignee", "Consignee Address",
          "Voucher Type", "Voucher No.", "Voucher Ref. No.", "Narration", "Quantity", "Alt. Units",
          "Rate", "Value", "Gross Total", "SALES GST LOCAL", "IGST (OUTPUT)", "Round Off",
          "CGST  Output", "SGST Output", "Rental Income"]


def _voucher(d, party, vtype, no, ref, narr, qty, alt, value, gross, ledgers=()):
    row = [d, party, party, "Addr", party, "Addr", vtype, no, ref, narr, qty, alt, None, value, gross]
    row += list(ledgers) + [None] * (len(HEADER) - len(row) - len(ledgers))
    return row


def _item(name, qty, alt, rate, value):
    return [None, name, "", "", "", "", "", "", "", "", qty, alt, rate, value] + [None] * (len(HEADER) - 14)


def _book(rows=None, grand_value=1300.0, header=HEADER) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales Register"
    for line in ["Candor Foods Private Limited", "W-202(A), MIDC,", "Sales Register", "1-Nov-26 to 30-Nov-26"]:
        ws.append([line])
    ws.append(header)
    d = datetime(2026, 11, 2)
    for row in rows if rows is not None else [
        _voucher(d, "Jagdish Trading Co (Sale)", "HO Sales", "CND/26-27/5557", "CF-SO/26-27/1459",
                 "Dispatch From 202.", 64, 32, 1000.0, 1050.0, (1000.0, None, None, 25.0, 25.0)),
        _item("King Solomon Medjoul Super Jumbo 500gm", 64, 32, 10.0, 640.0),
        _item("Smoked Almonds", 3, None, 120.0, 360.0),
        _voucher(d, "(cancelled )", "HO Sales", "CND/26-27/5625", "", "Qty. Revised.", None, None, None, None),
        _voucher(d, "Lattice Medtech", "HO SERVICE  GST", "SR/CF/26-27/359", "", "", None, None, None, 560.0,
                 (None, None, None, 42.75, 42.75, 475.0)),
        _voucher(d, "R K WorldInfocom", "HO Sales", "CND/26-27/5666", "PO/00852",
                 "New Invoice Is Raised, Against Old Invoice No.: CND/26-27/5625. Total Cartons : 5 ( A185 )",
                 10, 2.5, 300.0, 315.0),
        _item("Unknown Article XYZ", 10, 2.5, 30.0, 300.0),
    ]:
        ws.append(row)
    ws.append([None, "Grand Total"] + [""] * 11 + [grand_value, 1925.0])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


MASTER = [
    MasterItem(particulars="King Solomon Medjoul Super Jumbo 500gm", item_type="FG", group=None,
               sub_group=None, uom=0.5, sale_group=None, gst=None),
    MasterItem(particulars="Smoked Almonds", item_type="FG", group=None,
               sub_group=None, uom=1.0, sale_group=None, gst=None),
]

# The ERP's SOs: one numbered like ours, one numbered with the customer's PO.
SOS = {
    "CF-SO/26-27/1459": (1, [(101, "King Solomon Medjoul Super Jumbo 500gm"), (102, "Smoked Almonds")]),
    "PO/00852": (2, [(201, "Dried Blueberry 200g")]),
}


class _Conn:
    """Answers the upload's two SO lookups and records what a save writes."""

    def __init__(self, sos=None, tables=True, saved=()):
        self.sos = sos if sos is not None else {}
        self.tables = tables
        self.saved = set(saved)
        self.calls: list[tuple[str, tuple]] = []
        self.many: list[tuple[str, list]] = []

    def transaction(self):
        class _Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False
        return _Tx()

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if "FROM so_header" in sql:
            return [{"so_id": sid, "so_number": no} for no, (sid, _) in self.sos.items() if no.upper() in args[0]]
        if "FROM so_line" in sql:
            return [{"so_id": sid, "so_line_id": lid, "sku_name": name}
                    for sid, lines in self.sos.values() if sid in args[0] for lid, name in lines]
        if "FROM sales_invoice" in sql:
            return [{"invoice_no": n} for n in args[0] if n in self.saved]
        raise AssertionError(f"unexpected query: {sql}")

    async def fetchval(self, sql, *args):
        assert "to_regclass" in sql
        return self.tables

    async def execute(self, sql, *args):
        self.calls.append((sql, args))

    async def executemany(self, sql, rows):
        self.many.append((sql, list(rows)))


class _Pool:
    def __init__(self, **kw):
        self.conn = _Conn(**kw)

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                return False
        return _Ctx()


def _process(pool=None, book=None):
    return asyncio.run(process_sales_invoices(pool or _Pool(sos=SOS), book or _book(), MASTER))


# ── reading the file ─────────────────────────────────────────────────────────

def test_parses_vouchers_items_ledgers_and_the_preamble():
    p = parse_sales_invoices(_book())
    assert p["company"] == "Candor Foods Private Limited"
    assert p["period"] == "1-Nov-26 to 30-Nov-26"
    assert [i["voucher_no"] for i in p["invoices"]] == [
        "CND/26-27/5557", "CND/26-27/5625", "SR/CF/26-27/359", "CND/26-27/5666"]
    first = p["invoices"][0]
    assert first["quoted_so_number"] == "CF-SO/26-27/1459"
    assert first["dispatch_from"] == "W202"
    assert [ln["particulars"] for ln in first["lines"]] == [
        "King Solomon Medjoul Super Jumbo 500gm", "Smoked Almonds"]
    assert first["ledgers"] == {"SALES GST LOCAL": 1000.0, "CGST Output": 25.0, "SGST Output": 25.0}
    assert p["grand_total"] == {"value": 1300.0, "gross_total": 1925.0}


def test_cancelled_service_and_replacement_vouchers():
    inv = {i["voucher_no"]: i for i in parse_sales_invoices(_book())["invoices"]}
    assert inv["CND/26-27/5625"]["cancelled"] is True
    service = inv["SR/CF/26-27/359"]
    assert service["lines"] == [] and service["ledgers"]["Rental Income"] == 475.0
    assert service["voucher_type"] == "HO SERVICE GST"
    repl = inv["CND/26-27/5666"]
    assert repl["replaces_invoice"] == "CND/26-27/5625"
    assert repl["cartons"] == 5 and repl["dispatch_from"] == "A185"
    assert repl["quoted_so_number"] is None and repl["reference"] == "PO/00852"


@pytest.mark.parametrize("narration, site", [
    ("Dispatch From 202.", "W202"),
    ("Dispatch From 185, Approved By R.M. Patil Sir.", "A185"),
    ("Total Cartons : 50 ( W202 )", "W202"),
    ("A185", "A185"),
    ("Dispatch From A-68.", "A68"),
    ("Material dispatch from savla cold.", "savla cold"),
    ("Qty. Revised.", None),
    (None, None),
])
def test_dispatch_site_from_the_narration(narration, site):
    assert dispatch_site(narration) == site


def test_a_reference_written_like_our_so_numbers():
    assert so_number_of("CF-SO/26-27/1404") == "CF-SO/26-27/1404"
    assert so_number_of(" cf-so/26-27/9 ") == "CF-SO/26-27/9"
    for ref in ("PO/00852", "2KRZ4XSY", "MF-MH/PO/149/26-27", "Off-Grade", "", None):
        assert so_number_of(ref) is None


def test_a_file_that_is_not_a_sales_register_is_refused():
    wb = openpyxl.Workbook()
    wb.active.append(["Item", "Qty"])
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(ValueError, match="not a Tally Sales Register"):
        parse_sales_invoices(buf.getvalue())
    with pytest.raises(ValueError, match="Could not open"):
        parse_sales_invoices(b"not an excel file")


def test_a_sales_order_book_is_sent_to_the_first_upload():
    d = datetime(2026, 11, 2)
    book = _book(rows=[_voucher(d, "Rite Bite", "Sales Order", "CF-SO/26-27/1262", "", "", 10, 5, 100.0, 105.0),
                       _item("Something", 10, 5, 10.0, 100.0)], grand_value=100.0)
    with pytest.raises(ValueError, match="first section"):
        parse_sales_invoices(book)


# ── processing (read only) ───────────────────────────────────────────────────

def test_invoices_link_to_their_so_even_one_numbered_with_the_customers_po():
    pool = _Pool(sos=SOS)
    r = _process(pool)
    SalesInvoiceUploadResponse(**r)          # the endpoint's response model accepts it
    inv = {i["voucher_no"]: i for i in r["invoices"]}
    assert inv["CND/26-27/5557"]["so_number"] == "CF-SO/26-27/1459"
    assert inv["CND/26-27/5666"]["so_number"] == "PO/00852"          # SO numbered with the PO
    assert inv["SR/CF/26-27/359"]["so_found"] is False
    s = r["summary"]
    assert (s["linked_to_so"], s["sos_invoiced"], s["so_not_found"]) == (2, 2, 0)
    assert s["reference_not_an_so"] == 0 and s["without_reference"] == 1
    assert all(sql.lstrip().upper().startswith("SELECT") for sql, _ in pool.conn.calls), "checking must only read"


def test_items_are_tied_to_the_so_line_they_fulfil():
    r = _process()
    lines = {ln["particulars"]: ln for i in r["invoices"] for ln in i["lines"]}
    assert lines["King Solomon Medjoul Super Jumbo 500gm"]["so_line_id"] == 101
    assert lines["Smoked Almonds"]["so_line_id"] == 102
    unknown = lines["Unknown Article XYZ"]                      # PO/00852's SO has no such line
    assert unknown["so_line_id"] is None
    assert r["summary"]["lines_on_so_line"] == 2 and r["summary"]["lines_not_on_so_line"] == 1
    po_inv = next(i for i in r["invoices"] if i["voucher_no"] == "CND/26-27/5666")
    assert po_inv["issues"] == ["Line 1 (Unknown Article XYZ) is not on SO PO/00852."]


def test_articles_and_kg():
    r = _process()
    lines = {ln["particulars"]: ln for i in r["invoices"] for ln in i["lines"]}
    assert (lines["King Solomon Medjoul Super Jumbo 500gm"]["kg"],
            lines["King Solomon Medjoul Super Jumbo 500gm"]["kg_source"]) == (32, "alt_units")
    assert (lines["Smoked Almonds"]["kg"], lines["Smoked Almonds"]["kg_source"]) == (3.0, "master_uom")
    assert r["unmatched_articles"] == [{"particulars": "Unknown Article XYZ", "lines": 1}]
    s = r["summary"]
    assert (s["invoices"], s["cancelled"], s["lines"]) == (3, 1, 3)
    assert s["total_value"] == 1300.0 and s["total_kg"] == 32 + 3.0 + 2.5
    assert r["issues"] == []


def test_problems_are_reported_not_hidden():
    r = _process(_Pool(sos={}), _book(grand_value=9999.0))
    assert r["so_numbers_not_found"] == ["CF-SO/26-27/1459"]
    first = r["invoices"][0]
    assert first["so_found"] is False and first["issues"][0] == "SO CF-SO/26-27/1459 is not in the ERP."
    assert r["summary"]["reference_not_an_so"] == 1                     # PO/00852, no such SO
    assert any("Grand Total is 9,999.00" in i for i in r["issues"])


def test_voucher_value_must_equal_its_items_and_numbers_are_unique():
    d = datetime(2026, 11, 2)
    rows = [
        _voucher(d, "A", "HO Sales", "CND/1", "", "", 1, None, 500.0, 500.0),
        _item("Smoked Almonds", 1, None, 100.0, 100.0),
        _voucher(d, "B", "HO Sales", "CND/1", "", "", 1, None, 100.0, 100.0),
        _item("Smoked Almonds", 1, None, 100.0, 100.0),
        _voucher(d, "C", "HO Sales", "CND/2", "", "", None, None, None, 100.0),
    ]
    r = _process(_Pool(), _book(rows=rows, grand_value=600.0))
    assert "Voucher CND/1 appears 2 times in the file." in r["issues"]
    assert r["summary"]["duplicate_vouchers"] == 1
    by_no = [(i["voucher_no"], i["issues"]) for i in r["invoices"]]
    assert "Item values add up to 100.00, the voucher says 500.00." in by_no[0][1]
    assert by_no[2][1] == ["A sales voucher with no item lines."]


# ── saving ───────────────────────────────────────────────────────────────────

def test_save_writes_every_invoice_with_its_so_and_lines():
    r = _process()
    pool = _Pool(sos=SOS, saved={"CND/26-27/5557"})          # one already saved: updated, not doubled
    out = asyncio.run(save_sales_invoices(pool, r, uploaded_by="Planner", source_file="19112026SOBOOK.xlsx"))
    assert out == {"inserted": 3, "updated": 1, "lines": 3}
    (inv_sql, inv_rows), (line_sql, line_rows) = pool.conn.many
    assert "ON CONFLICT (invoice_no) DO UPDATE" in inv_sql
    by_no = {row[0]: row for row in inv_rows}
    assert by_no["CND/26-27/5557"][1] == "CF-SO/26-27/1459"          # so_number (the FK)
    assert by_no["CND/26-27/5666"][1] == "PO/00852"
    assert by_no["SR/CF/26-27/359"][1] is None
    assert by_no["CND/26-27/5625"][14] is True                        # cancelled kept, flagged
    assert json.loads(by_no["CND/26-27/5557"][19]) == {"SALES GST LOCAL": 1000.0, "CGST Output": 25.0,
                                                       "SGST Output": 25.0}
    assert by_no["CND/26-27/5557"][20:] == ("19112026SOBOOK.xlsx", "Planner")
    assert [(row[0], row[1], row[3]) for row in line_rows] == [
        ("CND/26-27/5557", 1, 101), ("CND/26-27/5557", 2, 102), ("CND/26-27/5666", 1, None)]
    deletes = [args for sql, args in pool.conn.calls if sql.startswith("DELETE FROM sales_invoice_line")]
    assert deletes == [(["CND/26-27/5557", "CND/26-27/5625", "SR/CF/26-27/359", "CND/26-27/5666"],)]


def test_save_refuses_a_file_with_a_voucher_twice():
    d = datetime(2026, 11, 2)
    rows = [_voucher(d, "A", "HO Sales", "CND/1", "", "", 1, None, 100.0, 100.0),
            _item("Smoked Almonds", 1, None, 100.0, 100.0),
            _voucher(d, "B", "HO Sales", "CND/1", "", "", 1, None, 100.0, 100.0),
            _item("Smoked Almonds", 1, None, 100.0, 100.0)]
    r = _process(_Pool(), _book(rows=rows, grand_value=200.0))
    pool = _Pool()
    with pytest.raises(ValueError, match="more than once"):
        asyncio.run(save_sales_invoices(pool, r, uploaded_by="x", source_file="f.xlsx"))
    assert pool.conn.many == []


def test_save_before_migration_141_says_so():
    pool = _Pool(tables=False)
    with pytest.raises(InvoiceTablesMissing):
        asyncio.run(save_sales_invoices(pool, _process(), uploaded_by="x", source_file="f.xlsx"))
    assert pool.conn.many == []


def test_migration_141_keys():
    sql = open("app/db/141_sales_invoices.sql", encoding="utf-8").read()
    assert "UNIQUE (so_number)" in sql
    assert "invoice_no         VARCHAR PRIMARY KEY" in sql
    assert "REFERENCES so_header(so_number)" in sql                  # many invoices -> one SO
    assert "REFERENCES sales_invoice(invoice_no) ON DELETE CASCADE" in sql
    assert "PRIMARY KEY (invoice_no, line_no)" in sql


# ── Pending Orders export ────────────────────────────────────────────────────

def _so_line(so_line_id, qty, rate=10.0, kg=None, so_number="CF-SO/26-27/1459", so_date=date(2026, 11, 1)):
    return {"so_number": so_number, "so_date": so_date, "company": "CFPL", "customer_name": "Jagdish",
            "so_line_id": so_line_id, "line_number": 1, "sku_name": f"Article {so_line_id}",
            "sales_group": "VA", "pack": "CTN", "quantity": qty, "rate_inr": rate,
            "amount_inr": qty * rate, "ordered_kg": kg}


def test_pending_is_ordered_minus_invoiced():
    today = date(2026, 11, 19)
    lines = [_so_line(1, 100, kg=50), _so_line(2, 10), _so_line(3, 5, so_number="8700942817"), _so_line(4, 8)]
    dispatches = {
        1: [{"invoice_no": "CND/1", "invoice_date": date(2026, 11, 5), "quantity": 30},
            {"invoice_no": "CND/2", "invoice_date": date(2026, 11, 9), "quantity": 20}],
        2: [{"invoice_no": "CND/3", "invoice_date": date(2026, 11, 6), "quantity": 10}],   # fully sent
        4: [{"invoice_no": "CND/4", "invoice_date": date(2026, 11, 7), "quantity": 9}],    # one too many
    }
    plans = {1: {"factory": "W-202", "floor": "Upper Basement", "person": "Harsh Arora"}}
    rows = pending_orders.pending_rows(lines, dispatches, plans, today)
    assert [r["Description"] for r in rows] == ["Article 1", "Article 3"]       # 2 and 4 are done
    a = rows[0]
    assert (a["Purchase Order Qty."], a["Dispatch Qty."], a["Pending Qty."]) == (100, 50, 50)
    assert a["Invoice No."] == "CND/1, CND/2" and a["Dispatch Date"] == date(2026, 11, 9)
    assert a["Days"] == 8 and a["Over Days"] == 18 and a["Ageing Days"] == "16-20"
    assert (a["Uom"], a["Total Kg"], a["UNITS/KGS"]) == (0.5, 25.0, "Units")
    assert (a["Line Fill"], a["Pending Value"], a["Dispatch Value"], a["In CR"]) == (0.5, 500.0, 500.0, 0.00005)
    assert (a["Packed to be"], a["Floor"], a["Person"]) == ("W-202", "Upper Basement", "Harsh Arora")
    assert a["PO No"] is None and rows[1]["PO No"] == "8700942817"
    assert rows[1]["Dispatch Qty."] is None and rows[1]["Pending Qty."] == 5

    every = pending_orders.pending_rows(lines, dispatches, plans, today, include_dispatched=True)
    over = next(r for r in every if r["Description"] == "Article 4")
    assert over["Pending Qty."] == 0 and over["Remark"] == "Invoiced 1 more than ordered"


@pytest.mark.parametrize("days, bucket", [(0, "0"), (1, "1-5"), (5, "1-5"), (6, "6-10"), (15, "11-15"),
                                          (20, "16-20"), (21, ">20"), (None, None)])
def test_ageing_buckets(days, bucket):
    assert pending_orders.ageing(days) == bucket


class _ReqPool:
    def acquire(self):
        class _Ctx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *exc):
                return False
        return _Ctx()


def _dashboard(monkeypatch, user):
    from types import SimpleNamespace
    from app.modules.so import router as so_router

    lines = [_so_line(1, 100, kg=50), _so_line(2, 10)]
    dispatches = {2: [{"invoice_no": "CND/3", "invoice_date": date(2026, 11, 6), "quantity": 10}]}

    async def _inputs(conn):
        return lines, dispatches, {}, True

    async def _status(conn):
        return {"tables": True, "invoices": 1, "lines_on_so_line": 1,
                "first_date": date(2026, 11, 6), "last_date": date(2026, 11, 6)}
    monkeypatch.setattr(so_router.pending_orders, "fetch_inputs", _inputs)
    monkeypatch.setattr(so_router.pending_orders, "invoice_status", _status)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=_ReqPool())))
    return asyncio.run(so_router.get_pending_orders(req, user=user))


def test_dashboard_data_keeps_dispatched_lines_and_drops_blank_cells(monkeypatch):
    from types import SimpleNamespace
    out = _dashboard(monkeypatch, SimpleNamespace(role_name="planner", is_admin=True))
    assert out["sees_cost"] is True and out["columns"] == pending_orders.COLUMNS
    assert [r["so_line_id"] for r in out["rows"]] == [1, 2]          # the fully sent line too
    assert out["rows"][1]["Pending Qty."] == 0 and out["rows"][1]["Invoice No."] == "CND/3"
    assert "PO Date" not in out["rows"][0]                            # blank cells aren't sent
    assert out["invoices"]["invoices"] == 1 and out["so_lines"] == 2


def test_money_columns_are_hidden_from_roles_that_may_not_see_cost(monkeypatch):
    from types import SimpleNamespace
    out = _dashboard(monkeypatch, SimpleNamespace(role_name="floor_operator", is_admin=False))
    assert out["sees_cost"] is False
    assert not set(out["columns"]) & pending_orders.COST_COLUMNS
    assert all(not set(r) & pending_orders.COST_COLUMNS for r in out["rows"])
    cols = pending_orders.visible_columns(False)
    ws = openpyxl.load_workbook(io.BytesIO(pending_orders.pending_orders_xlsx(
        pending_orders.pending_rows([_so_line(1, 5)], {}, {}, date(2026, 11, 19)),
        date(2026, 11, 19), columns=cols))).active
    assert [c.value for c in ws[2]] == cols and "Basic Rate" not in cols


def test_the_sheet_has_the_trackers_columns():
    today = date(2026, 11, 19)
    rows = pending_orders.pending_rows([_so_line(1, 100, kg=100)], {}, {}, today)
    ws = openpyxl.load_workbook(io.BytesIO(pending_orders.pending_orders_xlsx(rows, today))).active
    assert [c.value for c in ws[2]] == pending_orders.COLUMNS and len(pending_orders.COLUMNS) == 38
    row = {h: c.value for h, c in zip(pending_orders.COLUMNS, ws[3])}
    assert row["SO No"] == "CF-SO/26-27/1459" and row["Pending Qty."] == 100 and row["UNITS/KGS"] == "Kgs"
