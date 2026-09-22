"""Per-job-card BOM changes (migration 115).

A job card's BOM is the BOM module's bom_line rows for its bom_id -- read here,
never written. job_card_bom_change records what was removed from, or added to,
ONE job card: every stage card of its chain. scope_job_card_id is the chain's
first card, found by walking prev_job_card_id back while the plan line stays the
same (so partial chains -B2.. of one line, and a merged run's member packing
cards, are separate job cards). Everything that reads a job card's BOM goes
through here: get_job_card's bom_lines, Save Output, extra giveaway, floor
requisitions, receive-material, the PDF, the rebuild and merge paths.

Article identity is UPPER(BTRIM(name)), as in floor stock and requisitions.
Every query carries a /* jcbc:<tag> */ comment so tests can script a fake
connection by name.
Spec: docs/superpowers/specs/2026-09-21-job-card-bom-changes-design.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

import asyncpg

from app.core.helpers import insert_with_pk_retry, new_short_time_id

RM, PM = "rm", "pm"
FG, SFG = "fg", "sfg"
# The types a BOM-module line may be removed with.
ITEM_TYPES = (RM, PM)
# The types an article added to a job card may have (spec Addendum A). FG and
# SFG added articles are accounted as RM (kind_of); their required qty is kg.
ADDABLE_TYPES = (RM, PM, FG, SFG)
UNIT_FOR = {RM: "kg", PM: "pcs", FG: "kg", SFG: "kg"}
UOM_FOR = {RM: "KGS", PM: "PCS", FG: "KGS", SFG: "KGS"}
FINISHED = ("completed", "closed", "cancelled")
EGA_CONSOLIDATED = "CONSOLIDATED"
LIVE_INDEX = "uq_job_card_bom_change_live"


class BomChangeError(Exception):
    """A refusal: the router answers HTTP `http_status` with detail()."""

    def __init__(self, http_status: int, code: str, message: str, **details: Any):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
        self.details = details

    def detail(self) -> dict:
        return {"error": self.code, "message": self.message, **self.details}


def article_key(name: Optional[str]) -> str:
    return (name or "").strip().upper()


_SPACES = re.compile(r"\s+")


def loose_key(name: Optional[str]) -> str:
    """article_key with runs of spaces / non-breaking spaces collapsed: only for
    the add-time 'already on the BOM' check, never as an identity."""
    return _SPACES.sub(" ", (name or "").replace(" ", " ")).strip().upper()


def type_of(item_type: Optional[str]) -> str:
    return (item_type or "").strip().lower()


def kind_of(item_type: Optional[str]) -> str:
    """The accounting kind of an added article: PM stays PM, everything else
    (RM, FG, SFG) is RM input. Accounting treats every SFG article as the seam
    carried in from the previous stage, and consumption takes only RM/PM/SFG/WIP."""
    return PM if type_of(item_type) == PM else RM


def _json(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _row(r: Any) -> dict:
    return {k: _json(v) for k, v in dict(r).items()}


def _clean(text: Optional[str]) -> Optional[str]:
    t = (text or "").strip()
    return t[:500] or None


# ── SQL ─────────────────────────────────────────────────────────────────────

_TABLE_SQL = "/* jcbc:table */ SELECT to_regclass('public.job_card_bom_change') IS NOT NULL"

_CARD_SQL = """/* jcbc:card */
    SELECT job_card_id, job_card_number, plan_line_id, bom_id, status
      FROM job_card_v2
     WHERE job_card_id = $1 AND deleted_at IS NULL
"""

# Back through the previous stage while the plan line stays the same. Deleted
# cards are walked through (the chain's structure), the depth cap stops a cycle.
_HEAD_SQL = """/* jcbc:head */
    WITH RECURSIVE back AS (
        SELECT job_card_id, prev_job_card_id, plan_line_id, 0 AS depth
          FROM job_card_v2 WHERE job_card_id = $1
        UNION ALL
        SELECT p.job_card_id, p.prev_job_card_id, p.plan_line_id, b.depth + 1
          FROM back b
          JOIN job_card_v2 p ON p.job_card_id = b.prev_job_card_id
         WHERE p.plan_line_id = b.plan_line_id AND b.depth < 50
    )
    SELECT job_card_id FROM back ORDER BY depth DESC LIMIT 1
"""

_CHAIN_SQL = """/* jcbc:chain */
    WITH RECURSIVE fwd AS (
        SELECT job_card_id, plan_line_id, 0 AS depth FROM job_card_v2 WHERE job_card_id = $1
        UNION ALL
        SELECT c.job_card_id, c.plan_line_id, f.depth + 1
          FROM fwd f
          JOIN job_card_v2 c ON c.prev_job_card_id = f.job_card_id
         WHERE c.plan_line_id = f.plan_line_id AND f.depth < 50
    )
    SELECT j.job_card_id, j.job_card_number, j.status
      FROM fwd JOIN job_card_v2 j ON j.job_card_id = fwd.job_card_id
     WHERE j.deleted_at IS NULL
     ORDER BY fwd.depth, j.job_card_id
"""

_CHANGES_SQL = """/* jcbc:changes */
    SELECT change_id, change_type, material_sku_name, item_type, sku_id, required_qty,
           required_unit, note, made_on_job_card_id, made_on_job_card_number,
           changed_by, changed_at
      FROM job_card_bom_change
     WHERE scope_job_card_id = $1 AND undone_at IS NULL
     ORDER BY changed_at, change_id
"""

_MASTER_SQL = """/* jcbc:master */
    SELECT bom_line_id, line_number, material_sku_name, item_type,
           uom, quantity_per_unit, loss_pct, godown
      FROM bom_line
     WHERE bom_id = $1
     ORDER BY item_type, line_number
"""

_INDENTS_SQL = """/* jcbc:indents */
    SELECT bom_line_id, material_sku_name, 'rm' AS item_type, uom, loss_pct, godown
      FROM job_card_rm_indent_v2 WHERE job_card_id = $1
    UNION ALL
    SELECT bom_line_id, material_sku_name, 'pm', uom, loss_pct, godown
      FROM job_card_pm_indent_v2 WHERE job_card_id = $1
"""

_LOCK_UPDATE_SQL = """/* jcbc:lock_update */
    SELECT plan_line_id FROM production_plan_line_v2 WHERE plan_line_id = $1 FOR UPDATE
"""

_LOCK_SHARE_SQL = """/* jcbc:lock_share */
    SELECT plan_line_id FROM production_plan_line_v2 WHERE plan_line_id = $1 FOR KEY SHARE
"""

_LOCK_CARD_SHARE_SQL = """/* jcbc:lock_card_share */
    SELECT j.plan_line_id
      FROM job_card_v2 j
      JOIN production_plan_line_v2 l ON l.plan_line_id = j.plan_line_id
     WHERE j.job_card_id = $1
       FOR KEY SHARE OF l
"""

# Saved figures for one article on the chain's live cards. Zero rows and
# soft-deleted rows do not count. Issued indent qty is canonical input.
# A legacy no-batch consumption / off-grade row shows in Accounting under every
# batch that has no row of its own for the article (outputAccounting.ts), and
# can be cleared there. Once EVERY batch of the card has its own row, the
# legacy twin shows nowhere and can't be cleared from the web, so it does not
# count (a card with no batches shows it, so there it counts).
_RECORDS_SQL = """/* jcbc:records */
    WITH hits AS (
        SELECT 'consumption'::text AS kind, m.job_card_id, m.batch_id, m.actual_consumed_qty AS qty
          FROM job_card_material_consumption_v2 m
         WHERE m.job_card_id = ANY($1::bigint[])
           AND m.deleted_at IS NULL AND m.actual_consumed_qty > 0
           AND (UPPER(BTRIM(m.material_sku_name)) = $2 OR m.bom_line_id = ANY($3::int[]))
           AND NOT (m.batch_id IS NULL
                    AND EXISTS (SELECT 1 FROM job_card_batch_v2 bt WHERE bt.job_card_id = m.job_card_id)
                    AND NOT EXISTS (
                        SELECT 1 FROM job_card_batch_v2 bt
                         WHERE bt.job_card_id = m.job_card_id
                           AND NOT EXISTS (
                               SELECT 1 FROM job_card_material_consumption_v2 t
                                WHERE t.job_card_id = m.job_card_id AND t.batch_id = bt.batch_id
                                  AND (UPPER(BTRIM(t.material_sku_name)) = $2
                                       OR t.bom_line_id = ANY($3::int[])))))
        UNION ALL
        SELECT b.balance_type, b.job_card_id, b.batch_id, b.qty_kg
          FROM job_card_balance_material_v2 b
         WHERE b.job_card_id = ANY($1::bigint[])
           AND b.deleted_at IS NULL AND b.qty_kg > 0
           AND (UPPER(BTRIM(b.material_name)) = $2 OR b.bom_line_id = ANY($3::int[]))
        UNION ALL
        SELECT 'offgrade', y.job_card_id, y.batch_id, y.quantity
          FROM job_card_byproducts_v2 y
         WHERE y.job_card_id = ANY($1::bigint[])
           AND y.deleted_at IS NULL AND y.quantity > 0
           AND (UPPER(BTRIM(y.material_name)) = $2 OR y.bom_line_id = ANY($3::int[]))
           AND NOT (y.batch_id IS NULL
                    AND EXISTS (SELECT 1 FROM job_card_batch_v2 bt WHERE bt.job_card_id = y.job_card_id)
                    AND NOT EXISTS (
                        SELECT 1 FROM job_card_batch_v2 bt
                         WHERE bt.job_card_id = y.job_card_id
                           AND NOT EXISTS (
                               SELECT 1 FROM job_card_byproducts_v2 t
                                WHERE t.job_card_id = y.job_card_id AND t.batch_id = bt.batch_id
                                  AND t.category = y.category
                                  AND (UPPER(BTRIM(t.material_name)) = $2
                                       OR t.bom_line_id = ANY($3::int[])))))
        UNION ALL
        SELECT 'issued', i.job_card_id, NULL::bigint, i.issued_qty
          FROM job_card_rm_indent_v2 i
         WHERE i.job_card_id = ANY($1::bigint[]) AND i.issued_qty > 0
           AND UPPER(BTRIM(i.material_sku_name)) = $2
        UNION ALL
        SELECT 'issued', i.job_card_id, NULL::bigint, i.issued_qty
          FROM job_card_pm_indent_v2 i
         WHERE i.job_card_id = ANY($1::bigint[]) AND i.issued_qty > 0
           AND UPPER(BTRIM(i.material_sku_name)) = $2
    )
    SELECT h.kind, h.qty, h.job_card_id, j.job_card_number, h.batch_id,
           bt.batch_number, bt.status AS batch_status
      FROM hits h
      JOIN job_card_v2 j ON j.job_card_id = h.job_card_id
      LEFT JOIN job_card_batch_v2 bt ON bt.batch_id = h.batch_id
     ORDER BY j.job_card_number, h.batch_id NULLS FIRST, h.kind
"""


# ── scope and changes ───────────────────────────────────────────────────────

async def table_exists(conn) -> bool:
    return bool(await conn.fetchval(_TABLE_SQL))


@dataclass
class Scope:
    job_card_id: int
    job_card_number: str
    plan_line_id: int
    bom_id: Optional[int]
    scope_job_card_id: int
    cards: list[dict]

    @property
    def card_ids(self) -> list[int]:
        return [c["job_card_id"] for c in self.cards]

    @property
    def finished(self) -> bool:
        return bool(self.cards) and all(c["status"] in FINISHED for c in self.cards)


async def scope_of(conn, job_card_id: int) -> Optional[Scope]:
    card = await conn.fetchrow(_CARD_SQL, job_card_id)
    if card is None:
        return None
    head = await conn.fetchval(_HEAD_SQL, job_card_id) or job_card_id
    cards = [dict(r) for r in await conn.fetch(_CHAIN_SQL, head)]
    return Scope(job_card_id=card["job_card_id"], job_card_number=card["job_card_number"],
                 plan_line_id=card["plan_line_id"], bom_id=card["bom_id"],
                 scope_job_card_id=int(head), cards=cards)


@dataclass
class Changes:
    removed: list[dict] = field(default_factory=list)
    added: list[dict] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.removed and not self.added

    def by_key(self) -> dict[str, dict]:
        return {article_key(c["material_sku_name"]): c for c in [*self.removed, *self.added]}


async def load_changes(conn, scope_job_card_id: int) -> Changes:
    if not await table_exists(conn):
        return Changes()
    out = Changes()
    for r in await conn.fetch(_CHANGES_SQL, scope_job_card_id):
        row = dict(r)
        (out.removed if row["change_type"] == "removed" else out.added).append(row)
    return out


# ── the effective BOM ───────────────────────────────────────────────────────

def added_line(c: dict) -> dict:
    """An added article as a BOM line. item_type is its accounting kind (an added
    FG/SFG is 'rm'); article_type is its real type (spec Addendum A2)."""
    t = type_of(c["item_type"])
    k = kind_of(t)
    return {"bom_line_id": None, "line_number": None, "material_sku_name": c["material_sku_name"],
            "item_type": k, "article_type": t, "uom": UOM_FOR[k], "quantity_per_unit": None,
            "loss_pct": None, "godown": None, "added": True, "change_id": c["change_id"],
            "sku_id": c.get("sku_id"), "required_qty": _json(c.get("required_qty")),
            "required_unit": c.get("required_unit")}


def _indent_as_line(i: dict) -> dict:
    return {"bom_line_id": i.get("bom_line_id"), "line_number": None,
            "material_sku_name": i.get("material_sku_name"), "item_type": type_of(i.get("item_type")),
            "uom": i.get("uom"), "quantity_per_unit": None, "loss_pct": _json(i.get("loss_pct")),
            "godown": i.get("godown")}


def effective_bom_lines(master_lines: Iterable[dict], indent_lines: Iterable[dict],
                        changes: Changes) -> tuple[list[dict], dict[str, set[int]]]:
    """The card's BOM lines minus the removed articles plus the added ones.
    No changes -> the master lines unchanged (payload exactly as before)."""
    flags: dict[str, set[int]] = {"not_on_bom": set(), "superseded": set()}
    master = [dict(l) for l in master_lines]
    if changes.is_empty():
        return master, flags
    base = master or [_indent_as_line(dict(i)) for i in indent_lines]
    base_keys = {article_key(l.get("material_sku_name")) for l in base}
    removed_keys: set[str] = set()
    for c in changes.removed:
        k = article_key(c["material_sku_name"])
        removed_keys.add(k)
        if k not in base_keys:
            flags["not_on_bom"].add(c["change_id"])
    lines = [l for l in base if article_key(l.get("material_sku_name")) not in removed_keys]
    for c in changes.added:
        if article_key(c["material_sku_name"]) in base_keys:
            flags["superseded"].add(c["change_id"])
            continue
        lines.append(added_line(c))
    return lines, flags


def changes_payload(scope_job_card_id: int, changes: Changes, flags: dict[str, set[int]]) -> dict:
    def common(c: dict) -> dict:
        return {"change_id": c["change_id"], "material_sku_name": c["material_sku_name"],
                "item_type": type_of(c["item_type"]), "note": c.get("note"),
                "changed_by": c.get("changed_by"), "changed_at": _json(c.get("changed_at")),
                "made_on_job_card_number": c.get("made_on_job_card_number")}
    return {
        "scope_job_card_id": scope_job_card_id,
        "removed": [{**common(c), "not_on_bom": c["change_id"] in flags["not_on_bom"]}
                    for c in changes.removed],
        "added": [{**common(c), "sku_id": c.get("sku_id"), "required_qty": _json(c.get("required_qty")),
                   "required_unit": c.get("required_unit"),
                   "superseded": c["change_id"] in flags["superseded"]} for c in changes.added],
    }


async def detail_bom(conn, job_card_id: int, master_lines: list[dict],
                     rm_indents: list[dict], pm_indents: list[dict]) -> tuple[list[dict], Optional[dict]]:
    """get_job_card's bom_lines and bom_changes. Inputs are serialised dicts.
    Before migration 115 (or for a deleted card): the master lines, no block."""
    if not await table_exists(conn):
        return list(master_lines), None
    scope = await scope_of(conn, job_card_id)
    if scope is None:
        return list(master_lines), None
    changes = await load_changes(conn, scope.scope_job_card_id)
    indents = [{**r, "item_type": RM} for r in rm_indents] + [{**r, "item_type": PM} for r in pm_indents]
    lines, flags = effective_bom_lines(master_lines, indents, changes)
    return lines, changes_payload(scope.scope_job_card_id, changes, flags)


# ── saved figures ───────────────────────────────────────────────────────────

_KIND_LABEL = {"consumption": "consumption", "returned": "returned to store",
               "extra_given": "extra giveaway", "wastage": "wastage",
               "control_sample": "control sample", "offgrade": "off-grade"}


async def has_records(conn, scope: Scope, key: str, bom_line_ids: list[int]) -> list[dict]:
    rows = await conn.fetch(_RECORDS_SQL, scope.card_ids, key, [int(i) for i in bom_line_ids])
    return [_row(r) for r in rows]


def _qty_text(q: Any) -> str:
    return f"{float(q):g}"


def records_message(article: str, hits: list[dict]) -> str:
    parts: list[str] = []
    for h in hits:
        card = h.get("job_card_number")
        if h["kind"] == "issued":
            part = f"{_qty_text(h.get('qty'))} was received on {card} (return it to store first)"
        else:
            where = (f"batch {h.get('batch_number')}" if h.get("batch_id") is not None
                     else "saved without a batch")
            part = f"{_KIND_LABEL.get(h['kind'], h['kind'])} is saved on {card}, {where}"
            if h.get("batch_id") is not None and h.get("batch_status") not in (None, "open"):
                part += f" ({h.get('batch_status')}: an admin must re-open it with the override)"
        if part not in parts:
            parts.append(part)
    return (f"{article} can't be removed yet: " + "; ".join(parts) + ". "
            "Clear the figures in Accounting (clearing a figure saves 0), then remove it.")


# ── locks ───────────────────────────────────────────────────────────────────

async def lock_line(conn, plan_line_id: int, *, exclusive: bool) -> None:
    """The plan line row is the lock for BOM changes. It MUST be the first row
    lock the transaction takes (lock-order rule, spec 2c)."""
    await conn.fetchval(_LOCK_UPDATE_SQL if exclusive else _LOCK_SHARE_SQL, plan_line_id)


async def lock_line_for_card(conn, job_card_id: int) -> Optional[int]:
    """KEY SHARE on the card's plan line: taken by the paths that check an
    article against the job card's BOM and then write (Save Output,
    requisitions, receive-material). Does not conflict with other saves or
    their non-key updates; waits for a BOM change on the same line."""
    return await conn.fetchval(_LOCK_CARD_SHARE_SQL, job_card_id)


# ── writes (spec 2c) ────────────────────────────────────────────────────────

_SKU_SQL = "/* jcbc:sku */ SELECT sku_id, particulars, item_type FROM all_sku WHERE sku_id = $1"

# Add by name (Use on other floor stock). One name can have several SKUs (a floor
# item that is both FG and SFG): the type hint picks, else _TYPE_ORDER.
_SKU_BY_NAME_SQL = ("/* jcbc:sku_by_name */ SELECT sku_id, particulars, item_type FROM all_sku "
                    "WHERE UPPER(BTRIM(particulars)) = $1 ORDER BY sku_id")
_TYPE_ORDER = (RM, PM, SFG, FG)

# Before migration 116 these two checks refuse an FG/SFG row.
_NEED_116 = ("chk_jcbc_item_type", "chk_jcbc_required")

_INSERT_SQL = """/* jcbc:insert */
    INSERT INTO job_card_bom_change (
        change_id, scope_job_card_id, plan_line_id, change_type, material_sku_name, item_type,
        sku_id, required_qty, required_unit, note, made_on_job_card_id, made_on_job_card_number,
        changed_by)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
    RETURNING change_id
"""

_UNDO_SQL = """/* jcbc:undo */
    UPDATE job_card_bom_change SET undone_at = now(), undone_by = $2
     WHERE change_id = $1 AND undone_at IS NULL
"""

_CHANGE_SQL = """/* jcbc:change */
    SELECT change_id, change_type, material_sku_name, undone_at
      FROM job_card_bom_change
     WHERE change_id = $1 AND scope_job_card_id = $2
"""

_REQ_TABLE_SQL = "/* jcbc:req_table */ SELECT to_regclass('public.floor_requisition') IS NOT NULL"

_OPEN_REQS_SQL = """/* jcbc:open_reqs */
    SELECT requisition_id FROM floor_requisition
     WHERE job_card_id = ANY($1::bigint[]) AND UPPER(BTRIM(material_sku_name)) = $2
       AND status = 'raised'
     ORDER BY requisition_id
"""


@dataclass
class _State:
    scope: Scope
    changes: Changes
    master: list[dict]
    lines: list[dict]
    flags: dict[str, set[int]]


async def _state(conn, job_card_id: int) -> _State:
    scope = await scope_of(conn, job_card_id)
    if scope is None:
        raise BomChangeError(404, "job_card_not_found", f"Job card {job_card_id} does not exist.")
    changes = await load_changes(conn, scope.scope_job_card_id)
    master = ([_row(r) for r in await conn.fetch(_MASTER_SQL, scope.bom_id)]
              if scope.bom_id is not None else [])
    indents = [] if master else [_row(r) for r in await conn.fetch(_INDENTS_SQL, job_card_id)]
    lines, flags = effective_bom_lines(master, indents, changes)
    return _State(scope, changes, master, lines, flags)


async def _open(conn, job_card_id: int) -> _State:
    if not await table_exists(conn):
        raise BomChangeError(503, "bom_changes_not_available",
                             "BOM changes need database migration 115, which has not been applied yet.")
    card = await conn.fetchrow(_CARD_SQL, job_card_id)
    if card is None:
        raise BomChangeError(404, "job_card_not_found", f"Job card {job_card_id} does not exist.")
    # Lock first, then re-read the chain (lock-order rule, spec 2c).
    await lock_line(conn, card["plan_line_id"], exclusive=True)
    st = await _state(conn, job_card_id)
    if st.scope.finished:
        raise BomChangeError(409, "job_card_finished",
                             "Every stage of this job card is completed, closed or cancelled, "
                             "so its BOM can no longer be changed.")
    return st


async def _result(conn, job_card_id: int, action: str, *, restored: bool = False,
                  open_requisition_ids: Optional[list[int]] = None) -> dict:
    st = await _state(conn, job_card_id)
    return {"action": action, "restored": restored,
            "open_requisition_ids": open_requisition_ids or [],
            "bom_changes": changes_payload(st.scope.scope_job_card_id, st.changes, st.flags),
            "bom_lines": st.lines}


async def _insert(conn, scope: Scope, *, actor: str, change_type: str, name: str, item_type: str,
                  sku_id: Optional[int] = None, required_qty: Optional[Decimal] = None,
                  required_unit: Optional[str] = None, note: Optional[str] = None) -> int:
    async def _do():
        return await conn.fetchval(
            _INSERT_SQL, new_short_time_id(), scope.scope_job_card_id, scope.plan_line_id,
            change_type, name, item_type, sku_id, required_qty, required_unit, _clean(note),
            scope.job_card_id, scope.job_card_number, actor)
    try:
        return await insert_with_pk_retry(conn, _do)
    except asyncpg.UniqueViolationError as exc:
        if LIVE_INDEX in ((getattr(exc, "constraint_name", None) or "") + str(exc)):
            raise BomChangeError(409, "bom_changed_concurrently",
                                 "Someone changed this article on this job card just now. "
                                 "Reload and try again.") from None
        raise
    except asyncpg.CheckViolationError as exc:
        if _violates(exc, _NEED_116):
            raise BomChangeError(503, "bom_changes_need_116",
                                 "Adding FG or SFG articles needs database migration 116, "
                                 "which has not been applied yet.") from None
        raise


def _violates(exc: Exception, constraints: tuple[str, ...]) -> bool:
    """Whether a check violation names one of these constraints. Matched whole
    (quoted in the message): chk_jcbc_required must not match chk_jcbc_required_pair."""
    name = getattr(exc, "constraint_name", None) or ""
    if name:
        return name in constraints
    return any(f'"{c}"' in str(exc) for c in constraints)


async def _undo(conn, change_id: int, actor: str) -> None:
    status = await conn.execute(_UNDO_SQL, change_id, actor)
    if not str(status).endswith(" 1"):
        raise BomChangeError(409, "change_already_undone", "That change was already undone. Reload.")


async def _refuse_if_records(conn, scope: Scope, article: str, key: str, bom_line_ids: list[int]) -> None:
    hits = await has_records(conn, scope, key, bom_line_ids)
    if hits:
        raise BomChangeError(409, "article_has_records", records_message(article, hits), hits=hits)


async def open_requisitions(conn, scope: Scope, key: str) -> list[int]:
    if not await conn.fetchval(_REQ_TABLE_SQL):
        return []
    return [r["requisition_id"] for r in await conn.fetch(_OPEN_REQS_SQL, scope.card_ids, key)]


def parse_required(value: Any, item_type: str) -> tuple[Optional[Decimal], Optional[str]]:
    """An optional required qty: > 0, at most 3 decimals, whole for PM."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    def bad(msg: str) -> BomChangeError:
        return BomChangeError(422, "required_qty_invalid", msg)
    if isinstance(value, bool):
        raise bad("Enter the required quantity as a number.")
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise bad("Enter the required quantity as a number.") from None
    if not d.is_finite() or d <= 0:
        raise bad("The required quantity must be more than 0.")
    if d >= Decimal("1e11"):
        raise bad("That quantity is too large.")
    exp = d.normalize().as_tuple().exponent
    decimals = -exp if isinstance(exp, int) and exp < 0 else 0
    unit = UNIT_FOR[item_type]
    if unit == "pcs" and decimals > 0:
        raise bad("Pieces are whole numbers.")
    if decimals > 3:
        raise bad("Kilograms go to 3 decimals at most.")
    return d, unit


async def remove_article(conn, *, actor: str, job_card_id: int, material_sku_name: Optional[str],
                         note: Optional[str] = None) -> dict:
    st = await _open(conn, job_card_id)
    key = article_key(material_sku_name)
    on_list = [l for l in st.lines if key and article_key(l.get("material_sku_name")) == key]
    if not on_list:
        raise BomChangeError(422, "article_not_on_job_card",
                             f"{(material_sku_name or '').strip()!r} is not on this job card's BOM.")
    line = on_list[0]
    item_type = type_of(line.get("article_type") or line.get("item_type"))
    # An added article of any type can be removed (undo add); BOM-module lines
    # stay RM/PM only (the SFG seam line cannot be removed).
    if not line.get("added") and item_type not in ITEM_TYPES:
        raise BomChangeError(422, "not_rm_or_pm",
                             f"{line['material_sku_name']} is {item_type.upper() or 'untyped'}; "
                             "only RM and PM articles can be removed.")
    bom_ids = [int(l["bom_line_id"]) for l in st.master
               if article_key(l.get("material_sku_name")) == key and l.get("bom_line_id") is not None]
    await _refuse_if_records(conn, st.scope, line["material_sku_name"], key, bom_ids)
    if line.get("added"):
        await _undo(conn, line["change_id"], actor)
        action = "add_undone"
    else:
        superseded = st.changes.by_key().get(key)
        if superseded is not None:          # an added row the BOM has since caught up with
            await _undo(conn, superseded["change_id"], actor)
        await _insert(conn, st.scope, actor=actor, change_type="removed",
                      name=line["material_sku_name"], item_type=item_type, note=note)
        action = "removed"
    return await _result(conn, job_card_id, action,
                         open_requisition_ids=await open_requisitions(conn, st.scope, key))


def _not_addable(name: str, item_type: str) -> BomChangeError:
    return BomChangeError(422, "type_not_addable",
                          f"{name} is {item_type.upper() or 'untyped'}; "
                          "only RM, PM, FG and SFG articles can be added.")


async def _find_sku(conn, *, sku_id: Optional[int], material_sku_name: Optional[str],
                    item_type_hint: Optional[str]) -> Any:
    """The SKU to add: by sku_id, else by name (UPPER(BTRIM)). Of a name's addable
    SKUs, the one of the hinted type, else the first by _TYPE_ORDER then sku_id."""
    if sku_id is not None:
        sku = await conn.fetchrow(_SKU_SQL, sku_id)
        if sku is None:
            raise BomChangeError(404, "sku_not_found", f"SKU {sku_id} is not in the SKU master.")
        return sku
    rows = list(await conn.fetch(_SKU_BY_NAME_SQL, article_key(material_sku_name)))
    if not rows:
        raise BomChangeError(404, "sku_not_found",
                             f"{(material_sku_name or '').strip()} is not in the SKU master.")
    addable = [r for r in rows if type_of(r["item_type"]) in ADDABLE_TYPES]
    if not addable:
        raise _not_addable((rows[0]["particulars"] or "").strip(), type_of(rows[0]["item_type"]))
    addable.sort(key=lambda r: (_TYPE_ORDER.index(type_of(r["item_type"])), int(r["sku_id"])))
    hint = type_of(item_type_hint)
    return next((r for r in addable if type_of(r["item_type"]) == hint), addable[0])


def _refuse_unit_mismatch(name: str, item_type: str, item_type_hint: Optional[str],
                          required_qty: Any) -> None:
    """The client asked for the required qty in its hint's unit (pcs for PM, kg
    otherwise). When the hint picked nothing and the SKU master's type puts the
    qty in the other unit, refuse rather than store kg as pcs or pcs as kg."""
    hint = type_of(item_type_hint)
    if not hint or required_qty is None or (isinstance(required_qty, str) and not required_qty.strip()):
        return
    unit, asked = UNIT_FOR[kind_of(item_type)], UNIT_FOR[kind_of(hint)]
    if unit != asked:
        raise BomChangeError(409, "type_mismatch",
                             f"{name} is {item_type.upper()} in the SKU master, not {hint.upper()}, "
                             f"so its required qty is in {unit}, not {asked}. "
                             "Leave Required qty empty, or add it with + Add article.",
                             sku_item_type=item_type, required_unit=unit)


async def add_article(conn, *, actor: str, job_card_id: int, sku_id: Optional[int] = None,
                      material_sku_name: Optional[str] = None, item_type_hint: Optional[str] = None,
                      required_qty: Any = None, note: Optional[str] = None) -> dict:
    """Add an article by sku_id (+ Add article) or by name with an optional type
    hint (Use on other floor stock; spec Addendum A3). RM, PM, FG and SFG."""
    st = await _open(conn, job_card_id)
    sku = await _find_sku(conn, sku_id=sku_id, material_sku_name=material_sku_name,
                          item_type_hint=item_type_hint)
    name = (sku["particulars"] or "").strip()
    item_type = type_of(sku["item_type"])
    if item_type not in ADDABLE_TYPES:
        raise _not_addable(name, item_type)
    key = article_key(name)
    live = st.changes.by_key().get(key)
    stale_removal = None
    if live is not None and live["change_type"] == "removed":
        if live["change_id"] not in st.flags["not_on_bom"]:
            await _undo(conn, live["change_id"], actor)          # a removed BOM line: restore it
            return await _result(conn, job_card_id, "restored", restored=True)
        # The BOM module has since dropped the article, so undoing the removal
        # would bring nothing back: undo it and add the article (below).
        stale_removal = live
    for l in st.lines:
        spelled = l.get("material_sku_name")
        if article_key(spelled) == key or loose_key(spelled) == loose_key(name):
            raise BomChangeError(409, "already_on_job_card",
                                 f"{spelled} is already on this job card's BOM.", bom_spelling=spelled)
    _refuse_unit_mismatch(name, item_type, item_type_hint, required_qty)
    qty, unit = parse_required(required_qty, item_type)
    if stale_removal is not None:
        await _undo(conn, stale_removal["change_id"], actor)
    await _insert(conn, st.scope, actor=actor, change_type="added", name=name, item_type=item_type,
                  sku_id=int(sku["sku_id"]), required_qty=qty, required_unit=unit, note=note)
    return await _result(conn, job_card_id, "added")


async def undo_change(conn, *, actor: str, job_card_id: int, change_id: int) -> dict:
    st = await _open(conn, job_card_id)
    row = await conn.fetchrow(_CHANGE_SQL, change_id, st.scope.scope_job_card_id)
    if row is None:
        raise BomChangeError(404, "change_not_found", f"Change {change_id} is not on this job card.")
    if row["undone_at"] is not None:
        raise BomChangeError(409, "change_already_undone", "That change was already undone. Reload.")
    if row["change_type"] == "added" and row["change_id"] not in st.flags["superseded"]:
        await _refuse_if_records(conn, st.scope, row["material_sku_name"],
                                 article_key(row["material_sku_name"]), [])
    await _undo(conn, change_id, actor)
    restored = row["change_type"] == "removed"
    return await _result(conn, job_card_id, "restored" if restored else "add_undone", restored=restored)


# ── article checks for the write paths (spec 2d) ────────────────────────────

_CARD_BOM_SQL = "/* jcbc:card_bom */ SELECT bom_id FROM job_card_v2 WHERE job_card_id = $1"


def _removed_error(name: Optional[str]) -> dict:
    return {"error": "article_removed_from_job_card", "material_sku_name": name,
            "message": f"{(name or '').strip()} was removed from this job card's BOM."}


@dataclass
class ArticleResolver:
    master: dict[str, list[dict]]
    line_keys: dict[int, str]
    removed: dict[str, dict]
    added: dict[str, dict]

    @classmethod
    def build(cls, master_lines: Iterable[dict], changes: Changes) -> "ArticleResolver":
        master: dict[str, list[dict]] = {}
        line_keys: dict[int, str] = {}
        for l in master_lines:
            k = article_key(l.get("material_sku_name"))
            if not k:
                continue
            master.setdefault(k, []).append(dict(l))
            if l.get("bom_line_id") is not None:
                line_keys[int(l["bom_line_id"])] = k
        removed = {article_key(c["material_sku_name"]): c for c in changes.removed}
        added = {article_key(c["material_sku_name"]): c for c in changes.added
                 if article_key(c["material_sku_name"]) not in master}      # superseded -> the BOM line
        return cls(master, line_keys, removed, added)

    def state(self, name: Optional[str]) -> str:
        k = article_key(name)
        if not k:
            return "none"
        if k in self.removed:
            return "removed"
        if k in self.master:
            return "bom"
        if k in self.added:
            return "added"
        return "none"

    def is_removed(self, *, name: Optional[str] = None, bom_line_id: Any = None) -> bool:
        if bom_line_id is not None and self.line_keys.get(int(bom_line_id)) in self.removed:
            return True
        return bool(article_key(name)) and article_key(name) in self.removed

    def added_row(self, name: Optional[str]) -> Optional[dict]:
        return self.added.get(article_key(name))

    def canonical_name(self, name: Optional[str]) -> str:
        k = article_key(name)
        if k in self.master:
            return self.master[k][0]["material_sku_name"]
        if k in self.added:
            return self.added[k]["material_sku_name"]
        return (name or "").strip()

    def item_type(self, name: Optional[str]) -> Optional[str]:
        k = article_key(name)
        if k in self.master:
            return type_of(self.master[k][0].get("item_type"))
        if k in self.added:
            return kind_of(self.added[k]["item_type"])      # an added FG/SFG is RM input
        return None

    def check_consumption(self, lines: Iterable[dict]) -> Optional[dict]:
        lines = list(lines)
        ids = {int(l["bom_line_id"]) for l in lines if l.get("bom_line_id") is not None}
        invalid = ids - set(self.line_keys)
        if invalid:
            return {"error": "invalid_bom_line",
                    "message": f"bom_line_id(s) {sorted(invalid)} do not belong to this job card's BOM"}
        for l in lines:
            name = l.get("material_sku_name")
            bid = l.get("bom_line_id")
            if bid is not None:
                k = self.line_keys[int(bid)]
                if k in self.removed:
                    return _removed_error(self.master[k][0]["material_sku_name"])
                continue
            st = self.state(name)
            if st == "removed":
                return _removed_error(name)
            if st == "none":
                return {"error": "article_not_on_job_card", "material_sku_name": name,
                        "message": f"{(name or '').strip()!r} is not on this job card's BOM."}
        return None

    def check_rows(self, *, balance: Iterable[dict], byproducts: Iterable[dict]) -> Optional[dict]:
        for r in balance:
            if float(r.get("qty_kg") or 0) <= 0:
                continue
            name = r.get("material_name") or r.get("material_sku_name")
            if article_key(name) == EGA_CONSOLIDATED:
                continue
            if self.is_removed(name=name, bom_line_id=r.get("bom_line_id")):
                return _removed_error(name)
        for r in byproducts:
            if float(r.get("qty_kg") or 0) <= 0:
                continue
            if self.is_removed(name=r.get("material_name"), bom_line_id=r.get("bom_line_id")):
                return _removed_error(r.get("material_name"))
        return None

    def stored_entry(self, entry: dict) -> dict:
        """A line without a bom_line_id is stored under its canonical spelling; an
        added article's is stored with its accounting kind (an added FG is 'RM'
        consumption even if a client sends 'FG')."""
        if entry.get("bom_line_id") is None:
            out = {**entry, "material_sku_name": self.canonical_name(entry.get("material_sku_name"))}
            added = self.added_row(entry.get("material_sku_name"))
            if added is not None:
                out["input_kind"] = kind_of(added["item_type"]).upper()
            return out
        return entry


async def resolver_for(conn, job_card_id: int) -> ArticleResolver:
    bom_id = await conn.fetchval(_CARD_BOM_SQL, job_card_id)
    master = [_row(r) for r in await conn.fetch(_MASTER_SQL, bom_id)] if bom_id is not None else []
    if not master:
        master = [_indent_as_line(_row(r)) for r in await conn.fetch(_INDENTS_SQL, job_card_id)]
    changes = Changes()
    if await table_exists(conn):
        scope = await scope_of(conn, job_card_id)
        if scope is not None:
            changes = await load_changes(conn, scope.scope_job_card_id)
    return ArticleResolver.build(master, changes)


# ── requisitions and receive-material (spec 2f, 2g) ─────────────────────────

_LIVE_CHANGE_SQL = """/* jcbc:live_change */
    SELECT change_id, change_type, material_sku_name, item_type, sku_id, required_qty, required_unit
      FROM job_card_bom_change
     WHERE scope_job_card_id = $1 AND UPPER(BTRIM(material_sku_name)) = $2 AND undone_at IS NULL
"""

_REMOVED_KEYS_SQL = """/* jcbc:removed_keys */
    SELECT UPPER(BTRIM(material_sku_name)) AS k
      FROM job_card_bom_change
     WHERE scope_job_card_id = $1 AND change_type = 'removed' AND undone_at IS NULL
"""


async def live_change_for(conn, job_card_id: int, name: Optional[str]) -> Optional[dict]:
    """The live change for one article on this job card, or None."""
    key = article_key(name)
    if not key or not await table_exists(conn):
        return None
    scope = await scope_of(conn, job_card_id)
    if scope is None:
        return None
    row = await conn.fetchrow(_LIVE_CHANGE_SQL, scope.scope_job_card_id, key)
    return _row(row) if row is not None else None


async def removed_keys_for(conn, job_card_id: int) -> set[str]:
    if not await table_exists(conn):
        return set()
    scope = await scope_of(conn, job_card_id)
    if scope is None:
        return set()
    return {r["k"] for r in await conn.fetch(_REMOVED_KEYS_SQL, scope.scope_job_card_id)}


# ── rebuilt, relinked and merged job cards (spec 2i) ────────────────────────

def _walk_back(start: str) -> str:
    """_HEAD_SQL's walk for many cards at once: `heads` is the first card of the
    chain of every card matching `start`. A cancelled chain whose cards are all
    deleted has no live card to start from, so its first card is not a head here."""
    return f"""WITH RECURSIVE back AS (
        SELECT job_card_id AS start_id, job_card_id, prev_job_card_id, plan_line_id, 0 AS depth
          FROM job_card_v2 WHERE {start}
        UNION ALL
        SELECT b.start_id, p.job_card_id, p.prev_job_card_id, p.plan_line_id, b.depth + 1
          FROM back b
          JOIN job_card_v2 p ON p.job_card_id = b.prev_job_card_id
         WHERE p.plan_line_id = b.plan_line_id AND b.depth < 50
    ),
    heads AS (
        SELECT DISTINCT ON (start_id) job_card_id FROM back ORDER BY start_id, depth DESC
    )"""


_HEADS_SQL = "/* jcbc:heads */\n    " + _walk_back("job_card_id = ANY($1::bigint[])") + """
    SELECT DISTINCT job_card_id FROM heads ORDER BY job_card_id
"""

_LINE_CHANGES_SQL = """/* jcbc:line_changes */
    SELECT change_id, UPPER(BTRIM(material_sku_name)) AS k
      FROM job_card_bom_change
     WHERE plan_line_id = $1 AND scope_job_card_id = ANY($2::bigint[]) AND undone_at IS NULL
     ORDER BY changed_at, change_id
"""

_REBUILD_UNDO_SQL = """/* jcbc:rebuild_undo */
    UPDATE job_card_bom_change
       SET undone_at = now(), undone_by = 'system', undo_reason = 'rebuild'
     WHERE change_id = ANY($1::bigint[])
"""

_REPOINT_SQL = """/* jcbc:repoint */
    UPDATE job_card_bom_change SET scope_job_card_id = $2
     WHERE change_id = ANY($1::bigint[])
"""

# Live changes of the chains that still have a live card on these lines. A
# cancelled chain's changes can no longer be undone (its cards answer 404), so
# they must not hold a merge up for good.
_LINES_HELD_SQL = "/* jcbc:lines_held */\n    " + _walk_back(
    "plan_line_id = ANY($1::bigint[]) AND deleted_at IS NULL") + """
    SELECT DISTINCT c.made_on_job_card_number
      FROM job_card_bom_change c
     WHERE c.plan_line_id = ANY($1::bigint[]) AND c.undone_at IS NULL
       AND c.scope_job_card_id IN (SELECT job_card_id FROM heads)
     ORDER BY c.made_on_job_card_number
"""


async def chain_heads(conn, job_card_ids: Iterable[int]) -> list[int]:
    """The first card of each of these cards' chains (the scope their changes
    are stored against). Empty before migration 115: there is nothing to carry."""
    ids = [int(i) for i in job_card_ids]
    if not ids or not await table_exists(conn):
        return []
    return [int(r["job_card_id"]) for r in await conn.fetch(_HEADS_SQL, ids)]


async def repoint_after_rebuild(conn, *, plan_line_id: int, new_head: int,
                                old_heads: Iterable[int]) -> int:
    """The line's chains were rebuilt (replace_job_cards_for_line) or relinked
    under a new first card (apply_live_job_card_edits): carry the live changes of
    the chains that were live -- `old_heads`, read before the change -- over to
    the new first card. A cancelled chain's changes stay where they are. When two
    chains both changed one article, the earliest change is kept and the others
    are undone with undo_reason 'rebuild'. Returns how many changes carried over."""
    heads = sorted({int(h) for h in old_heads})
    if not heads or not await table_exists(conn):
        return 0
    heads = sorted(set(heads) | {int(new_head)})
    rows = await conn.fetch(_LINE_CHANGES_SQL, plan_line_id, heads)
    keep: dict[str, int] = {}
    drop: list[int] = []
    for r in rows:
        if r["k"] in keep:
            drop.append(r["change_id"])
        else:
            keep[r["k"]] = r["change_id"]
    if drop:
        await conn.execute(_REBUILD_UNDO_SQL, drop)
    if keep:
        await conn.execute(_REPOINT_SQL, list(keep.values()), new_head)
    return len(keep)


async def lines_with_live_changes(conn, plan_line_ids: list[int]) -> list[str]:
    """Job card numbers that hold live BOM changes on these plan lines' live chains."""
    if not plan_line_ids or not await table_exists(conn):
        return []
    return [r["made_on_job_card_number"] for r in await conn.fetch(_LINES_HELD_SQL, list(plan_line_ids))]
