"""Mint box ids for inward boxes that never got one, and attach them to a job card.

WHY THESE ROWS EXIST
--------------------
`box_id` is not written at inward time. `inward_tools.py:1825` inserts the box row
without one, and `:3449` says so outright: "New box — no box_id yet (assigned when
approver prints)". So a transaction whose labels were never printed has box rows
that are real stock but carry no scannable identity, and `interunit_tools.py:687`
guards every downstream read with `AND COALESCE(b.box_id, '') <> ''` — meaning
those boxes are invisible to the transfer, cold and scan paths alike.

Legacy solves this one box at a time: `upsert_box` (PUT /inward/{company}/{txno}/
box) has an "existing row without box_id -> UPDATE it" branch. This is that
operation in bulk, plus the job-card attach.

TWO ID FORMATS, BOTH COPIED FROM LEGACY
---------------------------------------
Which one applies depends on whether the row has a box_number at all — on the
pre-v2 `{p}_boxes` table it generally does NOT:

  box_number present, v2 table, line_number present   (upsert_box:3624)
      f"{base}-{line_number}-{box_number}"
  box_number present, otherwise                       (upsert_box:3624)
      f"{base}-{box_number}"
  NO box_number                                       (generate_box_ids:285-292)
      f"{base}-{i}"   -- i is a 1-based counter over the boxes minted in this call

`base` is the last 8 digits of epoch-ms in all three, shared across one call.

"PICK A BOX" IS DELIBERATELY NOT RANDOM
---------------------------------------
When rows carry no box_number they are fungible: same transaction, same article,
nothing to tell them apart. Labelling "a" box therefore means labelling an
ARBITRARY one — but arbitrary is not the same as random. This orders by the row
key and takes the first N, so the same call twice in a row does the same thing,
a support question has an answer, and a failed run can be reasoned about.
Randomness would buy nothing and cost all of that.

`limit` is how many to label; omit it to label every unlabelled row.

TARGETING EXACTLY ONE ROW WITHOUT A BOX NUMBER
----------------------------------------------
The UPDATE has to hit one row, and box_number cannot be the key here. It uses the
surrogate `id` when the table has one, and falls back to `ctid` otherwise —
which is safe here specifically because the rows were locked FOR UPDATE inside
this same transaction, so no VACUUM or concurrent UPDATE can move them under us.
If a row somehow has neither, the call refuses rather than issuing an unkeyed
UPDATE that would stamp the whole transaction with one id.

THE PHYSICAL CAVEAT — READ THIS BEFORE CALLING IT
-------------------------------------------------
An id minted here exists in the DATABASE but not on the CARTON. Until the label
is printed and stuck on, scanning that box still cannot find it; all this buys is
that the row becomes addressable and stops being filtered out of downstream
reads. `minted` is returned per box precisely so the caller can print them.

SAFETY
------
  * Every table and column is checked against information_schema first. If the
    column map does not hold, the call RAISES — it never guesses and never
    partially writes. {cfpl|cdpl}_boxes has no CREATE TABLE in this repo and only
    `lot_number` was ever evidenced from source, so this matters.
  * The UPDATE carries `box_id IS NULL OR box_id = ''` in its own WHERE, not just
    the SELECT, so an approver printing concurrently wins and is never clobbered.
  * Minting and stamping run in ONE transaction; the job-card attach runs after
    it commits, because scan_box must not be inside a transaction.
"""
from __future__ import annotations

import logging
import time

from app.modules.production.services.box_identify_service import _columns

logger = logging.getLogger(__name__)

# Company -> table prefix. A whitelist, because the prefix is interpolated into
# SQL; it is never taken from the caller's string directly.
_PREFIX = {"CFPL": "cfpl", "CDPL": "cdpl"}

# Preference order. v2 is the current shape; {p}_boxes is the pre-v2 table.
# Whichever holds rows for the transaction wins.
_CANDIDATES = ("{p}_boxes_v2", "{p}_boxes")

# The only columns the write path genuinely cannot work without. box_number and
# line_number are OPTIONAL — they select the id format, and the pre-v2 table
# frequently has neither.
_REQUIRED = ("box_id", "transaction_no")

_MAX_REMINT = 8


class BoxIdAssignError(Exception):
    """Raised instead of writing when the schema or input cannot be trusted."""


def _mint(base: str, *, seq: int, box_number=None, line_number=None,
          use_line: bool = False) -> str:
    """The three legacy formats. `seq` is the 1-based fallback counter."""
    if box_number is None:
        return f"{base}-{seq}"                                # generate_box_ids
    if use_line and line_number is not None:
        return f"{base}-{line_number}-{box_number}"           # upsert_box, v2
    return f"{base}-{box_number}"                             # upsert_box, legacy


def _bump(base: str, n: int) -> str:
    return str(int(base) + n).zfill(8)[-8:]


async def _pick_table(conn, prefix: str, transaction_no: str) -> tuple[str, frozenset[str]]:
    """First candidate whose columns check out AND which holds rows for this txn."""
    checked = []
    for tmpl in _CANDIDATES:
        table = tmpl.format(p=prefix)
        cols = await _columns(conn, table)
        if not cols:
            checked.append(f"{table}: absent")
            continue
        missing = set(_REQUIRED) - cols
        if missing:
            checked.append(f"{table}: missing {sorted(missing)}")
            continue
        if await conn.fetchval(
                f"SELECT 1 FROM {table} WHERE transaction_no = $1 LIMIT 1", transaction_no):
            return table, cols
        checked.append(f"{table}: no rows for this transaction")
    raise BoxIdAssignError(
        f"No usable box table for transaction {transaction_no!r} — " + "; ".join(checked))


async def assign_box_ids(conn, *, company: str, transaction_no: str,
                         job_card_id: int | None = None,
                         box_number: int | None = None,
                         limit: int | None = None,
                         scanned_by: str | None = None) -> dict:
    """Mint ids for this transaction's unlabelled boxes; optionally attach to a JC.

    `box_number` narrows to one carton where box numbers exist. `limit` caps how
    many rows are labelled — use limit=1 to label a single arbitrary box when the
    rows carry no box_number and are therefore indistinguishable. Omit both to
    label every unlabelled row under the transaction.

    Returns {"table", "key", "minted": [...], "attached", "skipped"}.
    Raises BoxIdAssignError when the schema or input cannot be trusted.
    """
    prefix = _PREFIX.get((company or "").strip().upper())
    if not prefix:
        raise BoxIdAssignError("company must be CFPL or CDPL")
    transaction_no = (transaction_no or "").strip()
    if not transaction_no:
        raise BoxIdAssignError("transaction_no is required")
    if limit is not None and limit < 1:
        raise BoxIdAssignError("limit must be 1 or more")

    table, cols = await _pick_table(conn, prefix, transaction_no)
    has_box_no = "box_number" in cols
    # The line component is a v2-only concept — mirrors inward_tools' _use_line,
    # `_is_v2_tables(tables) and line_number is not None`.
    use_line = table.endswith("_boxes_v2") and "line_number" in cols

    if box_number is not None and not has_box_no:
        raise BoxIdAssignError(
            f"{table} has no box_number column — drop the box_number filter and "
            "use limit to label an arbitrary box instead.")

    # Row key. `id` where the table has one; otherwise ctid, which is stable for
    # the life of this transaction because the rows are locked FOR UPDATE.
    key_expr, key_pred = ("id", "id = $3") if "id" in cols else ("ctid::text", "ctid = $3::tid")

    box_sel = "box_number" if has_box_no else "NULL::int AS box_number"
    line_sel = "line_number" if use_line else "NULL::int AS line_number"
    # Deterministic, not random: fungible rows still deserve a reproducible pick.
    order = "box_number, " if has_box_no else ""

    minted: list[dict] = []
    async with conn.transaction():
        preds = ["transaction_no = $1", "(box_id IS NULL OR box_id = '')"]
        args: list = [transaction_no]
        if box_number is not None:
            preds.append("box_number = $2")
            args.append(box_number)
        sql = (f"SELECT {key_expr} AS _key, {box_sel}, {line_sel} FROM {table} "
               f"WHERE {' AND '.join(preds)} ORDER BY {order}_key FOR UPDATE")
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        rows = await conn.fetch(sql, *args)
        if not rows:
            return {"table": table, "key": key_expr, "minted": [], "attached": [],
                    "skipped": 0}

        # Every id already in this table, so a fresh mint cannot land on one.
        # box_id is in no unique key and its 8-digit base repeats about every
        # 27.7 hours — a duplicate would make the box unresolvable by scan, which
        # is the whole thing this exists to fix.
        taken = {r["box_id"] for r in await conn.fetch(
            f"SELECT box_id FROM {table} WHERE box_id IS NOT NULL AND box_id <> ''")}

        base = str(int(time.time() * 1000))[-8:]
        for seq, r in enumerate(rows, start=1):
            bn, ln, key = r["box_number"], r["line_number"], r["_key"]
            cand = _mint(base, seq=seq, box_number=bn, line_number=ln, use_line=use_line)
            n = 0
            while cand in taken and n < _MAX_REMINT:
                n += 1
                cand = _mint(_bump(base, n), seq=seq, box_number=bn,
                             line_number=ln, use_line=use_line)
            if cand in taken:
                raise BoxIdAssignError(
                    f"Could not mint a free box_id for {transaction_no} after "
                    f"{_MAX_REMINT} attempts — refusing rather than writing a "
                    "duplicate that would make the box unresolvable by scan.")
            taken.add(cand)

            # The IS NULL guard is repeated HERE, not just in the SELECT above:
            # if the approver prints concurrently, their id must win.
            done = await conn.execute(
                f"UPDATE {table} SET box_id = $1 WHERE transaction_no = $2 "
                f"AND {key_pred} AND (box_id IS NULL OR box_id = '')",
                cand, transaction_no, key)
            if done.endswith(" 0"):
                continue  # lost the race; leave the printed id alone
            minted.append({"box_number": bn, "line_number": ln, "box_id": cand,
                           "row_key": str(key)})

    logger.info("box-id-assign: minted %d id(s) on %s (key=%s) for %s",
                len(minted), table, key_expr, transaction_no)

    attached: list[dict] = []
    if job_card_id and minted:
        # AFTER the commit: scan_box resolves through identify_box and must not
        # run inside a transaction (see its docstring).
        from app.modules.production.services.box_scan_service import scan_box
        for m in minted:
            res = await scan_box(conn, job_card_id=job_card_id, code=m["box_id"],
                                 scanned_by=scanned_by)
            attached.append({"box_id": m["box_id"], **(
                {"error": res["error"]} if res.get("error") else {"ok": True})})

    return {"table": table, "key": key_expr, "minted": minted,
            "attached": attached, "skipped": len(rows) - len(minted)}
