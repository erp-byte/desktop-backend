"""Which floors a job-card chain change touches, and who may touch them.

The rule (user decision, 2026-10-27):
  * people who can approve plans (planners) may put any floor of the plan's own
    factory on a chain;
  * everyone else with floor grants (floor managers, team leaders) only their own
    floors;
  * admins and people with no floor grants are not limited.

Only the floors a change TOUCHES are checked: the floors of the cards it adds,
moves (old and new floor) or removes. A quantity-only edit of a chain that runs
across someone else's floor touches none. Before this every chain route checked
every floor in the request, so such an edit was refused, while the floors of the
cards an edit removed or a replace deleted were never checked at all.

Floor names are checked against the factory's production floors (the declared
list in stock_take/floors.py, less its store floors), ignoring case, and stored in
the declared spelling. A floor a row already had is kept exactly as stored, odd
spelling and all. A factory that declares no floors accepts any name, as the
screens' free-text box always has.

Pure; the routes build a FloorGate (router._chain_floor_gate) and the services
call it before their first write, inside the route's transaction.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from app.core.warehouse_scope import normalise_floor, user_has_floor
from app.modules.stock_take.floors import FLOORS_BY_WAREHOUSE, normalise_warehouse

#: Declared floors that hold stock rather than run a production stage. The
#: job-card screens never offered them (web lib/floorChoices.ts PRODUCTION_FLOORS
#: is the declared list without these), so a card is not put on one now either.
#: "FG store" stays: the A185 packing route ends there.
STORE_FLOORS = frozenset({"STORE", "A185 STORES", "A185 STORES RACK", "A185 COLD"})


def production_floors(warehouse: str | None) -> list[str]:
    """The floors a job card can go to in this factory, declared spelling and
    order. [] when the factory declares none."""
    return [f for f in FLOORS_BY_WAREHOUSE.get(normalise_warehouse(warehouse), ())
            if normalise_floor(f) not in STORE_FLOORS]


def spell_floor(warehouse: str | None, floor: str | None, keep: Iterable[str | None] = ()) -> str | None:
    """How to store `floor`: as one of `keep` (the floors the row or chain already
    had) when it is one of them, else in the factory's declared spelling. None when
    the factory declares its floors and this is none of them. Blank stays ''."""
    name = str(floor or "").strip()
    key = normalise_floor(name)
    if not key:
        return ""
    for kept in keep:
        if kept is not None and normalise_floor(kept) == key:
            return kept
    declared = production_floors(warehouse)
    if not declared:
        return name
    return next((d for d in declared if normalise_floor(d) == key), None)


def touched_floors(before: Iterable[str | None], after: Iterable[str | None]) -> list[str]:
    """Floors that gain or lose a card when a chain goes from `before` to `after`,
    as multisets ignoring case: Roasting > Packing becoming Roasting > Sorting >
    Packing touches Sorting only, wherever it was inserted."""
    before, after = list(before), list(after)
    b = Counter(normalise_floor(f) for f in before if normalise_floor(f))
    a = Counter(normalise_floor(f) for f in after if normalise_floor(f))
    changed = (a - b) + (b - a)
    out: dict[str, str] = {}
    for f in after + before:
        key = normalise_floor(f)
        if key in changed and key not in out:
            out[key] = str(f).strip()
    return list(out.values())


def _names(floors: list[str]) -> str:
    return floors[0] if len(floors) == 1 else f"{', '.join(floors[:-1])} and {floors[-1]}"


def _factory(warehouse: str | None) -> str:
    return str(warehouse or "").strip() or "this factory"


def floors_of_user_in(user, warehouse: str | None) -> list[str]:
    """The user's floor grants that are production floors of this factory, in the
    declared spelling; every grant when the factory declares no floors."""
    grants = [str(g).strip() for g in (getattr(user, "allowed_floors", None) or []) if str(g).strip()]
    declared = production_floors(warehouse)
    if not declared:
        return grants
    return [d for d in declared if user_has_floor(grants, d)]


def floor_refusal(user, warehouse: str | None, touched: Iterable[str | None], *,
                  any_floor_in_factory: bool = False) -> dict | None:
    """The 403 refusal when `user` may not put work on, or take work off, one of
    the `touched` floors; None when they may. Admins, people with no floor grants
    and (with any_floor_in_factory) planners pass. Case-blind (user_has_floor)."""
    if getattr(user, "is_admin", False) or any_floor_in_factory:
        return None
    grants = [g for g in (getattr(user, "allowed_floors", None) or []) if str(g).strip()]
    if not grants:
        return None
    bad: list[str] = []
    for f in touched:
        name = str(f or "").strip()
        if (name and not user_has_floor(grants, name)
                and normalise_floor(name) not in {normalise_floor(x) for x in bad}):
            bad.append(name)
    if not bad:
        return None
    mine = floors_of_user_in(user, warehouse)
    verb = "is not one of your floors" if len(bad) == 1 else "are not your floors"
    if mine:
        rest = (f"Your floors in {_factory(warehouse)}: {', '.join(mine)}. Use one of those, "
                "or ask a planner to make this change.")
    else:
        rest = (f"None of your floors are in {_factory(warehouse)}. Ask a planner to make this "
                "change, or an admin to add the floor to your profile.")
    return {
        "error": "floor_not_allowed",
        "message": f"{_names(bad)} {verb}, so you can't put work there or take it off. {rest}",
        "details": {"requested": bad, "allowed_floors": mine},
    }


def unknown_floor_refusal(warehouse: str | None, unknown: list[str]) -> dict:
    """400 for floors that are not production floors of the plan's factory (a typo
    in a free-text box, or another factory's floor)."""
    floors = production_floors(warehouse)
    verb = "is not a production floor" if len(unknown) == 1 else "are not production floors"
    return {
        "error": "floor_not_in_factory",
        "message": (f"{_names(unknown)} {verb} of {_factory(warehouse)}. "
                    f"Pick one of: {', '.join(floors)}."),
        "details": {"requested": unknown, "floors": floors, "warehouse": warehouse},
    }


@dataclass(frozen=True)
class FloorGate:
    """One request's floor rules on one plan's factory.

    `user` None checks nobody's grants (a service called outside a route); a
    `warehouse` None, or one that declares no floors, accepts any floor name."""
    user: object = None
    warehouse: str | None = None
    any_floor_in_factory: bool = True

    def spell(self, floor: str | None, keep: Iterable[str | None] = ()) -> str | None:
        return spell_floor(self.warehouse, floor, keep)

    def refusal(self, touched: Iterable[str | None]) -> dict | None:
        if self.user is None:
            return None
        return floor_refusal(self.user, self.warehouse, touched,
                             any_floor_in_factory=self.any_floor_in_factory)

    def unknown(self, floors: list[str]) -> dict:
        return unknown_floor_refusal(self.warehouse, floors)

    def check_chain(self, before: Iterable[str | None],
                    after: Iterable[str | None]) -> tuple[list[str | None], dict | None]:
        """For a change that builds a chain (create) or rebuilds one (replace,
        merge): `after` as it will be stored, and the refusal, if any. `before` is
        every floor of the cards it deletes; the touched floors are the multiset
        difference, so floors the chain keeps are not checked."""
        before = [f for f in before if f is not None]
        after = list(after)
        spelled, unknown = [], []
        for f in after:
            s = self.spell(f, keep=before)
            if s is None:
                unknown.append(str(f).strip())
            spelled.append(f if s is None else s)
        if unknown:
            return spelled, self.unknown(list(dict.fromkeys(unknown)))
        return spelled, self.refusal(touched_floors(before, spelled))
