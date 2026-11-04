"""The PO commit writes header.consignee_address to po_header on insert and
update; an update without one keeps the stored address (COALESCE)."""
import asyncio
import re

from app.modules.purchase.services import po_commit

ADDRESS = "Plot 12, MIDC Turbhe, Navi Mumbai 400705"


class RecordingConn:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


def _bound(sql: str, args: tuple, column: str):
    """The value bound to `column` in an INSERT column list or an UPDATE SET."""
    m = re.search(rf"{column}\s*=\s*COALESCE\(\$(\d+)", sql)
    if m:
        return args[int(m.group(1)) - 1]
    cols = re.search(r"\(\s*(transaction_no.*?)\)\s*VALUES", sql, re.S).group(1)
    names = [c.strip() for c in cols.split(",")]
    return args[names.index(column)]


def _header_call(conn, verb):
    return next((sql, args) for sql, args in conn.calls if verb in sql and "po_header" in sql)


def test_insert_writes_consignee_address():
    conn = RecordingConn()
    asyncio.run(po_commit._insert_po(
        conn, transaction_no="TR-20261022120000", entity="cfpl",
        header={"po_number": "CF/PO/1", "vendor_supplier_name": "V",
                "consignee_address": ADDRESS},
        lines=[]))
    sql, args = _header_call(conn, "INSERT")
    assert _bound(sql, args, "consignee_address") == ADDRESS
    assert _bound(sql, args, "vendor_supplier_name") == "V"


def test_insert_without_address_binds_null():
    conn = RecordingConn()
    asyncio.run(po_commit._insert_po(
        conn, transaction_no="TR-20261022120000", entity="cfpl",
        header={"po_number": "CF/PO/1", "vendor_supplier_name": "V"}, lines=[]))
    sql, args = _header_call(conn, "INSERT")
    assert _bound(sql, args, "consignee_address") is None


def test_update_coalesces_consignee_address():
    conn = RecordingConn()
    asyncio.run(po_commit._update_po(
        conn, transaction_no="TR-20261022120000", entity="cfpl",
        header={"consignee_address": ADDRESS}, lines=[]))
    sql, args = _header_call(conn, "UPDATE")
    assert _bound(sql, args, "consignee_address") == ADDRESS
    assert "COALESCE($22, consignee_address)" in sql
    # entity stays the WHERE guard
    assert args[20] == "cfpl"
