"""SFG names on merge and step reorder (decided 2026-10-27).

Before:
  * the Merge process pop-up pre-filled the shared SFG with a made-up
    "SFG-<raw material>" — a code nothing knows — because validate_process_merge
    offered nothing, and that value also blocked the server's fallback to the
    product's routed SFG####;
  * the pop-up sent no pieces, so every packaging card of a merged run had no
    pieces target;
  * reordering or adding a plan step on a carded line cleared every SFG code on
    chains of 3+ cards and swapped a 2-card chain's code for the routed SFG####.

Now:
  * each merge group carries suggested_sfg — the products' own SFG (their
    cards' packing input, else their routing) when they agree, else their
    shared standard SFG, else None;
  * a member sent without pieces gets the plan line's planned pieces, scaled to
    its kg; a blank shared SFG still falls back to the routed SFG####;
  * the step resync keeps each un-started card's code; only a chain with no
    code at all gets the routed seam.
Services run against scripted connections; no database.
"""
from __future__ import annotations

import pytest

from app.modules.production.services import job_card_v2 as jcv2
from app.modules.production.services import plan_v2
from app.modules.production.services.sfg_canonical import _norm


# ── validate_process_merge: suggested_sfg ──────────────────────────────────

def _line(plan_line_id, fg, bom_id, *, carded=False):
    return {
        "plan_line_id": plan_line_id, "plan_id": 900, "fg_sku_name": fg, "bom_id": bom_id,
        "planned_qty_kg": 100, "planned_qty_units": 400, "plan_status": "approved",
        "factory": "a-185", "entity": "cfpl", "floor1": "roasting area",
        "rm_fingerprint": "almonds", "jc_count": 3 if carded else 0, "jc_started": 0,
        "has_cards": carded,
    }


class _ValidateConn:
    """The lines and steps of validate_process_merge, then what the members'
    cards feed packing, their routing, and their standard SFGs."""

    def __init__(self, lines, *, card_codes=None, routes=None, standard=None):
        self.lines = lines
        self.card_codes = card_codes or {}            # plan_line_id -> [packing input_code]
        self.routes = routes or {}                    # bom_id -> SFG####
        self.standard = {_norm(fg): s for fg, s in (standard or {}).items()}
        self.asked: list[str] = []

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        if "FROM production_plan_line_v2 l" in s:
            return self.lines
        if s.startswith("SELECT plan_line_id, step_order"):
            return [{"plan_line_id": ln["plan_line_id"], "step_order": 1, "process_name": "Roasting",
                     "stage": "roasting", "floor": "Roasting Area"} for ln in self.lines]
        if "FROM job_card_v2" in s and "output_kind = 'FG'" in s:
            self.asked.append("cards")
            return [{"plan_line_id": p, "codes": c} for p, c in self.card_codes.items() if p in args[0]]
        if "FROM bom_process_route" in s:
            self.asked.append("routes")
            return [{"bom_id": b, "output_code": c} for b, c in self.routes.items() if b in args[0]]
        raise AssertionError(s[:90])

    async def fetchval(self, sql, *args):
        assert "to_regclass('public.floor_requisition')" in sql, sql     # rebuild_refusal
        return True

    async def fetchrow(self, sql, *args):
        if "AS requested_on" in sql:              # rebuild_refusal: nothing blocks the merge
            return {"cancelled": 0, "requested_on": None, "amended_on": None}
        assert "FROM sfg_canonical_map" in sql, sql
        self.asked.append("standard")
        sfg = self.standard.get(args[0])
        return {"sfg_name": sfg} if sfg else None


async def _suggested(conn):
    out = await plan_v2.validate_process_merge(conn, [900])
    assert len(out["groups"]) == 1
    return out["groups"][0]["suggested_sfg"]


@pytest.mark.asyncio
async def test_products_routed_to_one_sfg_suggest_it():
    conn = _ValidateConn([_line(1, "Almonds 250g", 71), _line(2, "Almonds 1kg", 72)],
                         routes={71: "SFG0042", 72: "SFG0042"})
    assert await _suggested(conn) == "SFG0042"
    assert "standard" not in conn.asked                  # settled by the routing
    assert "cards" not in conn.asked                     # nothing carded to read


@pytest.mark.asyncio
async def test_routes_that_differ_fall_back_to_a_shared_standard_sfg():
    conn = _ValidateConn([_line(1, "Almonds 250g", 71), _line(2, "Almonds 1kg", 72)],
                         routes={71: "SFG0042", 72: "SFG0043"},
                         standard={"Almonds 250g": "Roasted Almonds", "Almonds 1kg": "Roasted Almonds"})
    assert await _suggested(conn) == "Roasted Almonds"


@pytest.mark.asyncio
async def test_nothing_shared_suggests_nothing():
    """No made-up 'SFG-<RM>': blank, so the planner types one or the merge uses
    the first product's routed SFG."""
    conn = _ValidateConn([_line(1, "Almonds 250g", 71), _line(2, "Cashews 250g", 72)],
                         routes={71: "SFG0042"},
                         standard={"Almonds 250g": "Roasted Almonds", "Cashews 250g": "Roasted Cashews"})
    assert await _suggested(conn) is None


@pytest.mark.asyncio
async def test_what_a_carded_products_cards_feed_packing_wins_over_its_routing():
    """Line 1 is carded (un-started): its packing opens with SFG0042, though its
    BOM now routes to SFG0099 — what its cards already use is its SFG."""
    conn = _ValidateConn([_line(1, "Almonds 250g", 71, carded=True), _line(2, "Almonds 1kg", 72)],
                         card_codes={1: ["SFG0042"]}, routes={71: "SFG0099", 72: "SFG0042"})
    assert await _suggested(conn) == "SFG0042"


@pytest.mark.asyncio
async def test_cards_feeding_two_sfgs_are_no_answer():
    conn = _ValidateConn([_line(1, "Almonds 250g", 71, carded=True), _line(2, "Almonds 1kg", 72)],
                         card_codes={1: ["SFG0042", "SFG0050"]}, routes={72: "SFG0042"})
    assert await _suggested(conn) is None              # line 1 falls back to no route


# ── create_merged_process_run: pieces backstop + routed fallback ───────────

def _member(plan_line_id, *, kg, units):
    return {"plan_line_id": plan_line_id, "plan_id": 900, "bom_id": 70 + plan_line_id,
            "fg_sku_name": f"FG {plan_line_id}", "customer_name": None,
            "planned_qty_kg": kg, "planned_qty_units": units,
            "factory": "a-185", "entity": "cfpl", "entity_raw": "cfpl", "warehouse_raw": "A-185",
            "plan_status": "approved", "floor1": "roasting area", "rm_fp": "almonds",
            "jc_started": 0, "has_cards": False}


class _Tx:
    async def __aenter__(self): return None
    async def __aexit__(self, *exc): return False


class _RunConn:
    """Every statement of create_merged_process_run; cards are recorded by number."""

    def __init__(self, members, *, routed="SFG0042"):
        self.members, self.routed = members, routed
        self.cards: dict[str, dict] = {}
        self._next = 1000

    def is_in_transaction(self): return True
    def transaction(self): return _Tx()

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        if "FOR UPDATE OF l" in s:
            assert "l.planned_qty_units" in s
            return self.members
        if s.startswith("SELECT floor FROM job_card_v2"):
            return []
        raise AssertionError(s[:90])

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        if "jcbc:table" in s:
            return False
        if "FROM bom_process_route" in s:
            return self.routed
        self._next += 1
        if s.startswith("INSERT INTO production_plan_step_v2"):
            return self._next
        if s.startswith("INSERT INTO job_card_v2"):
            self.cards[args[1]] = {"job_card_id": self._next, "planned_qty_units": args[13],
                                   "input_code": args[24], "output_code": args[25], "output_kind": args[16]}
            return self._next
        raise AssertionError(s[:90])

    async def execute(self, sql, *args):
        return "OK"


@pytest.fixture
def indents(monkeypatch):
    calls = []

    async def materialise(conn, **kw):
        calls.append((kw["job_card_id"], kw["planned_qty_units"], kw["include_pm"]))
        return (0, 0)
    monkeypatch.setattr(jcv2, "_materialise_indents", materialise)
    return calls


async def _run(conn, per_member, sfg=None):
    return await jcv2.create_merged_process_run(
        conn, plan_line_ids=[1, 2],
        wip_steps=[{"process": "Roasting", "floor": "Roasting Area", "sfg_output": sfg}],
        per_member=per_member)


@pytest.mark.asyncio
async def test_members_sent_without_pieces_get_the_lines_planned_pieces(indents):
    conn = _RunConn([_member(1, kg=100, units=400), _member(2, kg=50, units=200)])
    out = await _run(conn, [
        {"plan_line_id": 1, "pkg_floor": "Packing Area", "qty_kg": 80, "qty_units": None},   # kg edited
        {"plan_line_id": 2, "pkg_floor": "Packing Area", "qty_kg": 50, "qty_units": None},   # as planned
    ])
    assert "error" not in out, out
    pkg = {p["plan_line_id"]: conn.cards[f"MPG-{out['process_group_id']}-PK{p['plan_line_id']}"]
           for p in out["packaging"]}
    assert pkg[1]["planned_qty_units"] == 320.0                 # 400 pcs x 80/100 kg
    assert pkg[2]["planned_qty_units"] == 200.0
    # The PM indent of each packaging card is worked out from those pieces too.
    assert sorted(u for _jc, u, pm in indents if pm) == [200.0, 320.0]


@pytest.mark.asyncio
async def test_pieces_the_wizard_sends_are_kept(indents):
    conn = _RunConn([_member(1, kg=100, units=400), _member(2, kg=50, units=None)])
    out = await _run(conn, [
        {"plan_line_id": 1, "pkg_floor": "Packing Area", "qty_kg": 100, "qty_units": 390},
        {"plan_line_id": 2, "pkg_floor": "Packing Area", "qty_kg": 50, "qty_units": None},
    ])
    gid = out["process_group_id"]
    assert conn.cards[f"MPG-{gid}-PK1"]["planned_qty_units"] == 390.0
    assert conn.cards[f"MPG-{gid}-PK2"]["planned_qty_units"] is None     # the line plans no pieces


@pytest.mark.asyncio
async def test_a_blank_shared_sfg_uses_the_first_products_routed_code(indents):
    conn = _RunConn([_member(1, kg=100, units=400), _member(2, kg=50, units=200)], routed="SFG0042")
    out = await _run(conn, [{"plan_line_id": 1, "pkg_floor": "Packing Area"},
                            {"plan_line_id": 2, "pkg_floor": "Packing Area"}], sfg="  ")
    gid = out["process_group_id"]
    assert out["shared_sfg_code"] == "SFG0042"
    assert conn.cards[f"MPG-{gid}-P1"]["output_code"] == "SFG0042"
    assert conn.cards[f"MPG-{gid}-P1"]["output_kind"] == "SFG"
    assert {conn.cards[f"MPG-{gid}-PK{p}"]["input_code"] for p in (1, 2)} == {"SFG0042"}


@pytest.mark.asyncio
async def test_a_typed_shared_sfg_is_used_as_typed(indents):
    conn = _RunConn([_member(1, kg=100, units=400), _member(2, kg=50, units=200)], routed="SFG0042")
    out = await _run(conn, [{"plan_line_id": 1, "pkg_floor": "Packing Area"},
                            {"plan_line_id": 2, "pkg_floor": "Packing Area"}], sfg="Roasted Almonds")
    assert out["shared_sfg_code"] == "Roasted Almonds"


@pytest.mark.parametrize("units, planned_kg, qty_kg, want", [
    (400, 100, 100, 400.0),        # the planned kg: the planned pieces
    (400, 100, 80, 320.0),
    (400, 100, 33.3, 133.0),       # whole pieces
    (3, 2, 1, 2.0),                # half up, as the wizard's Math.round
    (12.5, 50, 50, 12.5),          # planned pieces kept as planned
    (None, 100, 80, None),
    (0, 100, 80, None),
    (400, None, 80, None),
    (400, 0, 80, None),
    (1, 100, 10, None),            # rounds to none
])
def test_scaled_units(units, planned_kg, qty_kg, want):
    assert jcv2._scaled_units(units, planned_kg, qty_kg) == want


# ── step reorder keeps the cards' SFG codes ────────────────────────────────

LINE = 7301


class _ReorderConn:
    """plan_v2.reorder_steps and the job-card resync it runs: plan steps whose
    order the UPDATEs change, the line's un-started cards, and every card write."""

    def __init__(self, cards, *, routed=None):
        # cards: (job_card_id, step_id, process, output_code) in chain order
        self.steps = {sid: {"step_id": sid, "step_order": i + 1, "process_name": p,
                            "stage": p.lower(), "floor": "Roasting Area"}
                      for i, (_jc, sid, p, _c) in enumerate(cards)}
        # One chain: each card hangs off the one before (several heads would be
        # several chains, which a step change refuses).
        self.cards = [{"job_card_id": jc, "plan_step_id": sid, "step_number": i + 1, "status": "locked",
                       "prev_job_card_id": cards[i - 1][0] if i else None,
                       "next_job_card_id": cards[i + 1][0] if i + 1 < len(cards) else None,
                       "output_code": code, "process_group_id": None}
                      for i, (jc, sid, _p, code) in enumerate(cards)]
        self.routed = routed
        self.writes: dict[int, dict] = {}
        self.asked_route = False

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("SELECT step_id FROM production_plan_step_v2"):
            return [{"step_id": sid} for sid in self.steps]
        if s.startswith("SELECT step_id, step_order"):
            return sorted(self.steps.values(), key=lambda r: r["step_order"])
        if "FROM job_card_v2" in s and "deleted_at IS NULL" in s:
            assert "output_code" in s
            return [dict(c) for c in self.cards]
        raise AssertionError(s[:90])

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        if "MAX(step_order)" in s:
            return max(r["step_order"] for r in self.steps.values())
        if s.startswith("SELECT bom_id FROM production_plan_line_v2"):
            return 71
        if "FROM bom_process_route" in s:
            self.asked_route = True
            return self.routed
        raise AssertionError(s[:90])

    async def execute(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("UPDATE production_plan_step_v2 SET step_order"):
            self.steps[args[0]]["step_order"] = args[1]
        elif s.startswith("UPDATE job_card_v2 SET step_number"):
            jc, step, _prev, in_kind, out_kind, *_rest, in_code, out_code = args
            self.writes[jc] = {"step": step, "in": (in_kind, in_code), "out": (out_kind, out_code)}
        elif not s.startswith("UPDATE job_card_v2 SET next_job_card_id"):
            raise AssertionError(s[:90])
        return "OK"


@pytest.mark.asyncio
async def test_reordering_a_three_card_chain_keeps_each_cards_sfg():
    """Sorting makes sorted almonds, Roasting roasted ones; swapping them moves
    the cards — each still makes what it made, and the next opens with it."""
    conn = _ReorderConn([(11, 21, "Sorting", "Sorted Almonds"),
                         (12, 22, "Roasting", "Roasted Almonds"),
                         (13, 23, "Packaging", None)], routed="SFG0042")
    out = await plan_v2.reorder_steps(conn, LINE, [22, 21, 23])
    assert out["jc_sync"]["applied"] is True
    assert conn.writes == {
        12: {"step": 1, "in": ("RM", None), "out": ("SFG", "Roasted Almonds")},
        11: {"step": 2, "in": ("SFG", "Roasted Almonds"), "out": ("SFG", "Sorted Almonds")},
        13: {"step": 3, "in": ("SFG", "Sorted Almonds"), "out": ("FG", None)},
    }
    assert conn.asked_route is False            # never re-derived from routing


@pytest.mark.asyncio
async def test_a_two_card_chain_keeps_its_standard_sfg_over_the_routed_code():
    conn = _ReorderConn([(11, 21, "Roasting", "Roasted Almonds"), (12, 22, "Packaging", None)],
                        routed="SFG0042")
    await plan_v2.reorder_steps(conn, LINE, [21, 22])
    assert conn.writes[11]["out"] == ("SFG", "Roasted Almonds")
    assert conn.writes[12]["in"] == ("SFG", "Roasted Almonds")
    assert conn.asked_route is False


@pytest.mark.asyncio
async def test_a_chain_with_no_code_still_gets_the_routed_seam():
    """Approved before Slice 3: no code anywhere, so the 2-card routed seam is
    stamped as before."""
    conn = _ReorderConn([(11, 21, "Roasting", None), (12, 22, "Packaging", None)], routed="SFG0042")
    await plan_v2.reorder_steps(conn, LINE, [21, 22])
    assert conn.writes[11]["out"] == ("SFG", "SFG0042")
    assert conn.writes[12] == {"step": 2, "in": ("SFG", "SFG0042"), "out": ("FG", None)}


@pytest.mark.asyncio
async def test_a_code_less_three_card_chain_stays_plain_wip():
    conn = _ReorderConn([(11, 21, "Sorting", None), (12, 22, "Roasting", None), (13, 23, "Packaging", None)],
                        routed="SFG0042")
    await plan_v2.reorder_steps(conn, LINE, [22, 21, 23])
    assert [conn.writes[j]["out"] for j in (12, 11, 13)] == [("WIP", None), ("WIP", None), ("FG", None)]
    assert all(w["in"][1] is None for w in conn.writes.values())
