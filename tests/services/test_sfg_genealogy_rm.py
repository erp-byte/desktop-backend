"""get_jc_genealogy's "produced" list is what the job card made. Boxes store
printed for the job card's requisitions (sfg_box item_type 'rm', migration 113)
carry its job_card_id but were not produced by it."""
from __future__ import annotations

import asyncio

from app.modules.production.services import sfg_box_service as sbs


class _Conn:
    def __init__(self):
        self.sql: list[str] = []

    async def fetch(self, sql, *args):
        self.sql.append(" ".join(sql.split()))
        return []


def test_produced_leaves_out_store_boxes():
    conn = _Conn()
    asyncio.run(sbs.get_jc_genealogy(conn, 75009889))
    produced = next(s for s in conn.sql if "WHERE job_card_id = $1" in s)
    assert "item_type <> 'rm'" in produced


def test_consumed_is_unchanged():
    conn = _Conn()
    asyncio.run(sbs.get_jc_genealogy(conn, 75009889))
    consumed = next(s for s in conn.sql if "received_into_job_card_id = $1" in s)
    assert "item_type" not in consumed.split("WHERE", 1)[1]
