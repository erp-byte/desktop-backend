"""The one site table: every warehouse, cold store and legacy site label the
ERP knows, with every spelling of each.

The data is ``sites.json`` next to this file. The frontend reads a
byte-identical copy (frontend/src/lib/sites.json, behind src/lib/sites.ts,
which mirrors this module except scope_keys); tests/services/test_sites.py
fails when the two copies differ, so edit both.

Every lookup goes by ``site_key`` -- ``warehouse_scope.normalise_warehouse_code``:
upper case with every non-alphanumeric dropped -- so 'W-202', 'w 202' and
'W202' are one spelling, and only real aliases ('Warehouse A185', 'Old Savla',
the grant code 'D-39' for 'Savla D-39') need a place in the table.

The rules, as the user decided them on 2026-10-16:
  * Compare sites with ``same_site``, never with ``==`` on stored strings.
  * 'Cold Storage' is a site of its own. It is cold in kind (``is_cold_site``),
    but it is never the same site as Rishi, Savla D-39 or any other store, and
    it is never a To.
  * 'Savla Bond' is a cold store of its own, not a spelling of Savla D-39.
  * A spelling the table does not know (bare 'Savla', say) stays an unknown
    site: ``canonical_site`` returns it trimmed, and it is the same site only
    as spellings with the same key.

In SQL, bind ``scope_keys(...)`` into ``warehouse_scope.WAREHOUSE_NORM_ANY_SQL``
so a lock or a filter matches every spelling of each site::

    conds.append(WAREHOUSE_NORM_ANY_SQL("h.from_site", f"${idx}"))
    params.append(scope_keys(user.allowed_warehouses))
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from app.core.warehouse_scope import normalise_warehouse_code

#: Upper case, every non-alphanumeric dropped; '' for None / empty. The very
#: same function as ``warehouse_scope.normalise_warehouse_code``.
site_key = normalise_warehouse_code

_DATA_FILE = Path(__file__).with_name("sites.json")
_KINDS = frozenset({"warehouse", "cold", "unknown"})
_COMPANIES = frozenset({"cfpl", "cdpl"})


@dataclass(frozen=True)
class _Site:
    canonical: str
    kind: str                  # 'warehouse' | 'cold' | 'unknown'
    company: Optional[str]     # 'cfpl' | 'cdpl' for a cold store, else None
    grant_code: Optional[str]  # what the admin screen grants; None = not grantable
    display: str
    address: str
    destination: bool          # offered as a transfer To
    spellings: tuple[str, ...]  # canonical, display, grant code, then the aliases
    key: str                   # site_key(canonical)


def _load(path: Path) -> tuple[tuple[_Site, ...], dict[str, _Site]]:
    """Read the table and index every spelling by its key. A malformed row, or
    a key claimed by two sites, raises: one spelling must never name two sites."""
    rows = json.loads(path.read_text(encoding="utf-8"))["sites"]
    sites: list[_Site] = []
    by_key: dict[str, _Site] = {}
    for row in rows:
        canonical = (row["canonical"] or "").strip()
        kind, company = row["kind"], row["company"]
        if not site_key(canonical):
            raise ValueError(f"{path.name}: a site has no canonical name")
        if kind not in _KINDS:
            raise ValueError(f"{path.name}: {canonical!r} has kind {kind!r}")
        if company is not None and (kind != "cold" or company not in _COMPANIES):
            raise ValueError(f"{path.name}: {canonical!r} has company {company!r}")
        spellings: list[str] = []
        for raw in (canonical, row["display"], row["grant_code"], *row["aliases"]):
            spelling = (raw or "").strip()
            if spelling and spelling not in spellings:
                spellings.append(spelling)
        site = _Site(
            canonical=canonical,
            kind=kind,
            company=company,
            grant_code=(row["grant_code"] or "").strip() or None,
            display=(row["display"] or "").strip() or canonical,
            address=(row["address"] or "").strip(),
            destination=bool(row["destination"]),
            spellings=tuple(spellings),
            key=site_key(canonical),
        )
        for spelling in spellings:
            key = site_key(spelling)
            if not key:
                raise ValueError(f"{path.name}: {canonical!r} has the empty spelling {spelling!r}")
            owner = by_key.setdefault(key, site)
            if owner is not site:
                raise ValueError(
                    f"{path.name}: {spelling!r} is a spelling of both "
                    f"{owner.canonical!r} and {canonical!r}"
                )
        sites.append(site)
    return tuple(sites), by_key


_SITES, _BY_KEY = _load(_DATA_FILE)


def _site(raw: Optional[str]) -> Optional[_Site]:
    return _BY_KEY.get(site_key(raw))


def _canonical_key(raw: Optional[str]) -> str:
    site = _site(raw)
    return site.key if site is not None else site_key(raw)


def canonical_site(raw: Optional[str]) -> str:
    """The canonical name of a known spelling ('Warehouse A185' -> 'A185',
    'D-39' -> 'Savla D-39', 'Supreme Cold' -> 'Supreme'); anything else comes
    back trimmed, and None / empty gives ''."""
    site = _site(raw)
    if site is not None:
        return site.canonical
    return (raw or "").strip()


def same_site(a: Optional[str], b: Optional[str]) -> bool:
    """True when ``a`` and ``b`` name one site: 'A185' / 'Warehouse A185',
    'D-39' / 'Savla D-39', 'Supreme Cold' / 'Supreme'. An empty value never
    matches, not even another empty value."""
    key = _canonical_key(a)
    return bool(key) and key == _canonical_key(b)


def is_cold_site(raw: Optional[str]) -> bool:
    """True for the cold stores and for the legacy 'Cold Storage' label. A floor
    such as 'A185 Cold' is a warehouse; an unknown name is not cold."""
    site = _site(raw)
    return site is not None and site.kind == "cold"


def cold_company(raw: Optional[str]) -> Optional[str]:
    """'cfpl' or 'cdpl' -- whose ``{company}_cold_stocks`` table holds a cold
    store's rows. None for a warehouse, for 'Cold Storage' (no one store) and
    for an unknown name."""
    site = _site(raw)
    return site.company if site is not None and site.kind == "cold" else None


def cold_row_site(unit: Optional[str], location: Optional[str]) -> str:
    """The cold store a cold-stock row ({cfpl,cdpl}_cold_stocks) is at, by its
    canonical name: its ``unit`` when that names a cold store, else its
    ``storage_location`` when that does; '' when neither does (a bare 'Savla', a
    chamber, 'Cold Storage'). Unit first, as IMS' canonical_warehouse_fn reads a
    row and as the frontend's coldRowSite does, so every reader puts a row in the
    same store: unit 'D-39' with storage_location 'Savla Bond' is at Savla D-39."""
    for raw in (unit, location):
        if cold_company(raw):
            return canonical_site(raw)
    return ""


def site_kind(raw: Optional[str]) -> str:
    """'warehouse', 'cold' or 'unknown' (an unknown name is 'unknown')."""
    site = _site(raw)
    return site.kind if site is not None else "unknown"


def site_aliases(raw: Optional[str]) -> list[str]:
    """Every known spelling of the site ``raw`` names -- canonical name, display
    name and grant code included. An unknown name is its own only spelling;
    None / empty gives []."""
    site = _site(raw)
    if site is not None:
        return list(site.spellings)
    value = (raw or "").strip()
    return [value] if value else []


def scope_keys(sites: Optional[Iterable[Optional[str]] | str]) -> list[str]:
    """``site_key`` of every spelling of every site given, deduped, in order.
    Bind it into ``WAREHOUSE_NORM_ANY_SQL`` so a lock or a filter on
    ['D-39'] also matches rows stored 'Savla D-39', 'Old Savla' or
    'Savla D-39 Cold Storage'. A lone string counts as one site."""
    if isinstance(sites, str):
        sites = [sites]
    keys: dict[str, None] = {}
    for raw in sites or ():
        for spelling in site_aliases(raw):
            key = site_key(spelling)
            if key:
                keys.setdefault(key, None)
    return list(keys)


def display_site(raw: Optional[str]) -> str:
    """The name printed on the challan ('Supreme' -> 'Supreme Cold Storage',
    'W202' -> 'Warehouse W202'); an unknown name comes back trimmed."""
    site = _site(raw)
    return site.display if site is not None else (raw or "").strip()


def site_address(raw: Optional[str]) -> str:
    """The challan address; '' when the site or its address is not known."""
    site = _site(raw)
    return site.address if site is not None else ""


_WAREHOUSE_SITES = tuple(s.canonical for s in _SITES if s.kind == "warehouse")
_COLD_SITES = tuple(s.canonical for s in _SITES if s.kind == "cold" and s.destination)
_TO_SITES = tuple(s.canonical for s in _SITES if s.destination)
_GRANTABLE_CODES = tuple(s.grant_code for s in _SITES if s.grant_code)


def warehouse_sites() -> list[str]:
    """The warehouses: W202, A185, A101, A68, F53."""
    return list(_WAREHOUSE_SITES)


def cold_sites() -> list[str]:
    """The cold stores, by canonical name. 'Cold Storage' is not one of them:
    it is cold in kind (``is_cold_site``) but is no store to send to or grant."""
    return list(_COLD_SITES)


def to_sites() -> list[str]:
    """Every site a transfer may be sent to: the warehouses, then the cold stores."""
    return list(_TO_SITES)


def grantable_codes() -> list[str]:
    """The codes the admin screen grants (auth_user.allowed_warehouses):
    W202, A185, ..., 'D-39' for Savla D-39, 'Savla Bond', 'Eskimo'."""
    return list(_GRANTABLE_CODES)
