"""Customer-return create: rtv_date and created_ts are stamped explicitly.

IMS's create_rtv wrote rtv_date = NOW() and created_ts = NOW(); the ERP insert
left both to legacy column defaults that may not exist, which would leave a new
return undated (out of every date filter, '—' on screen). The fake connection of
test_cr_dates_read stores NOW() as a UTC session writes it into a naive column and
leaves any column the INSERT omits NULL, so a dropped stamp fails here.

The id is minted on the IST clock while NOW() is stored as UTC; read back, the
two must name the same day (a 02:00 IST return is CR-20261019... and dated 19 Oct).

Run:  PYTHONPATH=. python -m pytest tests/services/test_cr_dates_create.py
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import create_service
from tests.services import test_cr_dates_read as rd
from tests.services.test_cr_dates_read import client  # noqa: F401  (the HTTP fixture)

MAKER = "maker@candorfoods.in"


class _Clock(datetime):
    """create_service's clock, frozen at 02:00 IST on 19 Oct 2026 (= rd.NOW_UTC)."""

    @classmethod
    def now(cls, tz=None):
        instant = rd.NOW_UTC.replace(tzinfo=timezone.utc)
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)


def _payload() -> schemas.CRCreate:
    return schemas.CRCreate(
        company="CFPL",
        header=schemas.CRHeaderCreate(factory_unit="W202", customer="ACME FOODS", invoice_number="INV-1"),
        lines=[schemas.CRLineCreate(material_type="RM", item_category="Nuts", sub_category="Almond",
                                    item_description="ALMOND W-320", uom="KG", qty="4", rate="10")],
    )


def _header_insert(conn) -> tuple[dict, tuple]:
    """The header INSERT as {column: value expression} and its bound arguments."""
    [(_kind, sql, args)] = [c for c in conn.calls if c[1].startswith("INSERT INTO cfpl_rtv_header ")]
    m = re.fullmatch(r"INSERT INTO cfpl_rtv_header \((.+?)\) VALUES \((.+?)\) RETURNING id", sql)
    assert m, sql
    cols, vals = [c.strip() for c in m[1].split(",")], [v.strip() for v in m[2].split(",")]
    assert len(cols) == len(vals)
    return dict(zip(cols, vals)), args


def test_create_stamps_rtv_date_and_created_ts_with_now(monkeypatch):
    monkeypatch.setattr(create_service, "datetime", _Clock)
    conn = rd.FakeConn()
    out = asyncio.run(create_service.create_cr(conn, "CFPL", _payload(), MAKER))

    cols, args = _header_insert(conn)
    assert cols.get("rtv_date") == "NOW()" and cols.get("created_ts") == "NOW()", cols
    assert cols["status"] == "'Pending'"
    # Every other column is still a bound parameter, $1..$16 each used once.
    params = sorted(int(v[1:]) for v in cols.values() if v.startswith("$"))
    assert params == list(range(1, 17)) and len(args) == 16
    bound = {c: args[int(v[1:]) - 1] for c, v in cols.items() if v.startswith("$")}
    assert bound["rtv_id"] == "CR-20261019020000" and bound["created_by"] == MAKER
    assert (bound["factory_unit"], bound["customer"], bound["invoice_number"]) == ("W202", "ACME FOODS", "INV-1")

    # Read back: the naive-UTC NOW() is the IST day and time the id names.
    assert out["rtv_id"] == "CR-20261019020000"
    assert out["rtv_date"].isoformat() == "2026-10-19T02:00:00+05:30"
    assert out["created_ts"].isoformat() == "2026-10-19T02:00:00+05:30"
    assert out["rtv_date"].strftime("%Y%m%d%H%M%S") == out["rtv_id"][len("CR-"):]


def test_a_new_night_shift_return_is_listed_on_its_ist_day(monkeypatch):
    monkeypatch.setattr(create_service, "datetime", _Clock)
    conn = rd.FakeConn()
    cr_id = asyncio.run(create_service.create_cr(conn, "CFPL", _payload(), MAKER))["rtv_id"]
    on = lambda day: [r["rtv_id"] for r in rd._list(conn, from_date=day, to_date=day)["records"]]  # noqa: E731
    assert on("19-10-2026") == [cr_id]
    assert on("18-10-2026") == []                    # the UTC date it is stored under


def test_the_create_response_carries_ist_offsets(client, monkeypatch):
    monkeypatch.setattr(create_service, "datetime", _Clock)
    res = client(rd.FakeConn()).post("/api/v1/customer-returns/CFPL", json=_payload().model_dump())
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["rtv_id"] == "CR-20261019020000"
    assert body["rtv_date"] == "2026-10-19T02:00:00+05:30"
    assert body["created_ts"] == "2026-10-19T02:00:00+05:30"
    assert [l["item_description"] for l in body["lines"]] == ["ALMOND W-320"]
