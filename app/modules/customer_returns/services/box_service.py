"""Customer-Returns box operations: single-box Print upsert, bulk box sync,
and the box-edit audit log. Boxes are keyed by (rtv_id, article_description,
box_number); box_id is NULL until Print and never regenerated once set.

A printed box's label is on a carton, so every write protects it:
  * a save never deletes a box that has a label; one left out of the list is kept
    (kept_printed) — only unprinted boxes are removed by leaving them out;
  * each print and save names the label the page believes every box has (box_id;
    None = unprinted). A box whose stored label differs — printed on another page, or
    a number reused after a cancelled edit — is refused (409 box_changed) instead of
    being overwritten, so one label can never end up on two cartons;
  * the page also sends the box-set fingerprint it loaded (base_version,
    query_service.box_version). If the boxes changed since, the write is refused (409
    boxes_changed) instead of putting back old values; each successful write returns
    the new fingerprint for the page to carry on with.

On an approved cold return every print and save also brings its cartons' cold-stock
rows up to date, in the same transaction (cold_stock_service.sync).

Every box write is also checked and recorded here, not left to the screen:
  * its weights and count (validate.box): never negative, net no more than gross;
  * a new label's id is one no box table holds yet (_mint_box_id), so a scan of it
    lands on this box only;
  * what changed on a printed box, and every first print, goes to box_edit_logs with
    who did it (_log_changes); a save's reply lists the printed boxes whose label now
    shows old values (reprint) so the screen can have them reprinted. The screen's own
    log call is checked against the return's boxes (log_box_edits);
  * an approved return is "Approved · Boxed" (Submitted) exactly while every article
    on it has a box and every box has a label (_set_boxed_status).
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException

from app.core.sites import is_cold_site
from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import approval_service
from app.modules.customer_returns.services import cold_stock_service, validate
from app.modules.customer_returns.services import query_service as q
from app.modules.customer_returns.tables import cr_table_names
from app.modules.production.services import box_identify_service


# Cold-storage warehouses (Savla D-39 / D-514, Rishi, Supreme, Eskimo, Savla Bond). IMS
# copied a cold return's boxes into {prefix}_cold_stocks on every box save
# (rtv_tools.sync_cold_stocks_from_rtv); since it closed on 2026-10-19 the ERP keeps that
# copy (cold_stock_service), and a cold return's boxes are entered here like any other.
# The one exception is a return IMS already copied: its boxes stay as they are, so the
# cold stock rows IMS made from them don't drift (cold_stock_service.ims_rows). A cold
# site is one of the site table (app.core.sites.is_cold_site), in any spelling: 'D-39',
# 'Savla D-39', 'Supreme Cold', 'Old Savla', 'Savla Bond'. A warehouse floor such as
# 'A185 Cold' is not one.
def _is_cold_warehouse(factory_unit) -> bool:
    return is_cold_site(str(factory_unit) if factory_unit else None)


async def _resolve_box_writable_header(conn, tables: dict, cr_id: str) -> tuple[int, str | None]:
    """(header_id, factory_unit) for a box write; 404 if absent. A cold return whose
    boxes IMS copied into cold stock is 409 cold_stock_from_ims: changing them here
    would leave those cold stock rows wrong."""
    row = await conn.fetchrow(
        f"SELECT id, factory_unit FROM {tables['header']} WHERE rtv_id = $1", cr_id)
    if row is None:
        raise HTTPException(
            404,
            detail={"error": "customer_return_not_found",
                    "message": f"No customer return {cr_id}", "details": {"rtv_id": cr_id}},
        )
    await refuse_ims_cold(conn, tables, cr_id, row["factory_unit"])
    return row["id"], row["factory_unit"]


async def refuse_ims_cold(conn, tables: dict, cr_id: str, factory_unit) -> None:
    """409 cold_stock_from_ims for a cold return whose boxes IMS copied into cold
    stock. The lookup plans around a missing table, so it may run inside a transaction."""
    if not _is_cold_warehouse(factory_unit):
        return
    copied = await cold_stock_service.ims_rows(conn, tables, cr_id)
    if copied:
        raise HTTPException(
            409,
            detail={"error": "cold_stock_from_ims",
                    "message": f"{cr_id}'s boxes were entered in IMS and IMS put them in cold "
                               f"stock ({copied} rows), so they can't be changed here.",
                    "details": {"rtv_id": cr_id, "factory_unit": factory_unit,
                                "cold_stock_rows": copied}},
        )


async def _sync_cold_stock(conn, company: str, cr_id: str, status, factory_unit) -> None:
    """After a box write: an approved cold return's cartons into cold stock."""
    if approval_service.is_approved(status) and cold_stock_service.cold_store(factory_unit):
        await cold_stock_service.sync(conn, company, cr_id)


def _label(v) -> str | None:
    """A box id as stored or sent: blank is no label."""
    v = (str(v) if v is not None else "").strip()
    return v or None


def _box_changed(cr_id: str, message: str, **details) -> HTTPException:
    return HTTPException(409, detail={"error": "box_changed", "message": message,
                                      "details": {"rtv_id": cr_id, **details}})


def _assert_same_label(cr_id: str, article: str, number: int, *, sent, stored, exists: bool) -> None:
    """The page's idea of a box's label must be the stored one. Otherwise a save or print
    would overwrite a box printed elsewhere (or one whose number was reused after a
    cancelled edit) and put its label on a second carton."""
    if sent == stored:
        return
    where = dict(article_description=article, box_number=number, box_id=stored, sent_box_id=sent)
    if stored:
        raise _box_changed(cr_id, f"Box {number} of {article} already has printed label {stored}, "
                                  f"printed after this page was opened. Reload {cr_id} to see it; "
                                  "nothing was saved.", **where)
    if exists:
        raise _box_changed(cr_id, f"Box {number} of {article} no longer has label {sent}. "
                                  f"Reload {cr_id} to see its boxes; nothing was saved.", **where)
    raise _box_changed(cr_id, f"Box {number} of {article} (label {sent}) is no longer on {cr_id}. "
                              "Reload to see its boxes; nothing was saved.", **where)


async def assert_base_version(conn, tables: dict, header_id: int, cr_id: str, base) -> None:
    """Refuse a write made from a page whose box list is out of date (409 boxes_changed).
    No base (an older client): no check; the per-box label check still applies."""
    if base is None:
        return
    current = await q.box_version(conn, tables, header_id)
    if current != base:
        raise HTTPException(
            409,
            detail={"error": "boxes_changed",
                    "message": f"The boxes on {cr_id} were changed on another page after this one "
                               "was opened. Reload to see them; nothing was saved.",
                    "details": {"rtv_id": cr_id}},
        )


async def _assert_boxes_open(conn, tables: dict, header_id: int, cr_id: str):
    """Refuse a box write on a Rejected return (409 rejected_return); else its status.
    Boxes stay open while it is undecided (the New screen prints them before sending)
    and after it is approved (the warehouse's "save boxes after approval" step, which
    keeps it approved). Read FOR SHARE inside the caller's transaction, so a rejection
    landing at the same moment waits for this write instead of slipping in after the
    check."""
    row = await conn.fetchrow(
        f"SELECT status FROM {tables['header']} WHERE id = $1 FOR SHARE", header_id)
    if row is None:  # deleted since the existence check: a 404, not a failed insert
        raise HTTPException(
            404,
            detail={"error": "customer_return_not_found",
                    "message": f"No customer return {cr_id}", "details": {"rtv_id": cr_id}},
        )
    if approval_service.is_rejected(row["status"]):
        raise HTTPException(
            409,
            detail={"error": "rejected_return",
                    "message": f"{cr_id} was rejected, so its boxes can't be changed.",
                    "details": {"rtv_id": cr_id}},
        )
    return row["status"]


def _base8() -> str:
    """Last 8 digits of epoch-milliseconds — the box_id prefix."""
    return str(int(time.time() * 1000))[-8:]


def _gen_single_box_id(box_number: int) -> str:
    """Single-print box_id: '{base8}-{box_number}' (two parts)."""
    return f"{_base8()}-{box_number}"


async def _mint_box_id(conn, box_number: int, tries: int = 20) -> str:
    """A new label id no box table holds yet. The 8-digit base repeats about every 28
    hours and other modules mint the same shape, so an unchecked id could be another
    carton's, and a scan of it would land on either. Read outside the box transaction:
    the lookup plans around missing tables but must not run inside one (box_identify)."""
    for _ in range(tries):
        cand = _gen_single_box_id(box_number)
        if not await box_identify_service.box_id_taken(conn, cand):
            return cand
        await asyncio.sleep(0.002)       # the next millisecond gives the next base
    raise HTTPException(
        503, detail={"error": "box_id_unavailable",
                     "message": "Couldn't find a free label id just now. Try the print again.",
                     "details": {"box_number": box_number}})


# What a box row holds besides its identity, and which of it is on the printed label.
_LABEL_FIELDS = ("net_weight", "gross_weight", "count", "lot_number", "item_mark")
_TRACKED = _LABEL_FIELDS + ("vakkal", "spl_remarks", "uom", "conversion")
_BOX_COLS = "id, article_description, box_number, box_id, " + ", ".join(_TRACKED)


def _same_value(a, b) -> bool:
    if isinstance(a, (Decimal, int, float)) and isinstance(b, (Decimal, int, float)):
        return Decimal(str(a)) == Decimal(str(b))
    return a == b


def _changes(stored, sent) -> dict:
    """{field: (old, new)} a write makes to a stored box. A field the write leaves
    out (None) keeps its stored value, as the UPDATE's COALESCE does."""
    out = {}
    for f in _TRACKED:
        new = getattr(sent, f, None)
        if new is not None and not _same_value(stored[f], new):
            out[f] = (stored[f], new)
    return out


async def _log_changes(conn, cr_id: str, box_id: str, changes: dict, actor: str) -> None:
    """One box_edit_logs row per change, as the edit-log endpoint writes them; skipped
    where the table doesn't exist (a dev database)."""
    if not changes or not await conn.fetchval("SELECT to_regclass('public.box_edit_logs') IS NOT NULL"):
        return
    edited_at = datetime.now(timezone.utc)
    for field, (old, new) in changes.items():
        old_t = "" if old is None else str(old)
        new_t = "" if new is None else str(new)
        description = (f"Printed label {new_t}" if field == "printed"
                       else f"Changed {field} from '{old_t}' to '{new_t}'")
        await conn.execute(
            """
            INSERT INTO box_edit_logs
                (email_id, description, transaction_no, box_id, field_name,
                 old_value, new_value, edited_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            """,
            actor or "", description, cr_id, box_id, field, old_t, new_t, edited_at,
        )


async def _set_boxed_status(conn, tables: dict, header_id: int, cr_id: str) -> None:
    """An approved return is Submitted ("Approved · Boxed") exactly while every article
    on it has at least one box and every box has a label; otherwise Approved. Only
    between those two: an undecided or rejected return is never touched."""
    boxed = await conn.fetchval(
        f"""
        SELECT EXISTS (SELECT 1 FROM {tables['lines']} WHERE header_id = $1)
           AND NOT EXISTS (SELECT 1 FROM {tables['lines']} l WHERE l.header_id = $1
                             AND NOT EXISTS (SELECT 1 FROM {tables['boxes']} b
                                              WHERE b.header_id = $1
                                                AND b.article_description = l.item_description))
           AND NOT EXISTS (SELECT 1 FROM {tables['boxes']} WHERE header_id = $1
                             AND NULLIF(btrim(box_id), '') IS NULL)
        """, header_id)
    await conn.execute(
        f"UPDATE {tables['header']} SET status = $2, updated_at = NOW() "
        "WHERE rtv_id = $1 AND btrim(status) IN ('Approved', 'Submitted') AND btrim(status) <> $2",
        cr_id, "Submitted" if boxed else "Approved",
    )


async def _resolve_line_id(conn, tables: dict, header_id: int, article: str):
    """The legacy line id for a box's article (box.article_description ==
    line.item_description) — set on the box's rtv_line_id FK so IMS's box->line join
    holds. None when the article has no matching line (mirrors the orphan boxes IMS
    itself carries)."""
    return await conn.fetchval(
        f"SELECT id FROM {tables['lines']} WHERE header_id = $1 AND item_description = $2 LIMIT 1",
        header_id, article,
    )


async def upsert_box(conn, company: str, cr_id: str,
                     payload: schemas.CRBoxUpsertRequest, actor: str = "") -> dict:
    """Print/print-edit a single box. A fresh box_id is minted; on an existing row
    the stored box_id is preserved so an already-printed box keeps its id and an
    unprinted one gets the new id. Every mutable field uses COALESCE(new, existing)
    so a None payload value never nulls a stored value. A CR-level advisory lock
    serializes concurrent box writes for the same CR (the legacy _rtv_boxes has no
    natural-key unique constraint to make an ON CONFLICT atomic)."""
    validate.box(payload)
    tables = cr_table_names(company)
    header_id, factory_unit = await _resolve_box_writable_header(conn, tables, cr_id)
    line_id = await _resolve_line_id(conn, tables, header_id, payload.article_description)

    # A page that names no label is printing it for the first time (one that names
    # one is reprinting it, and keeps it): only then is a new id needed.
    new_box_id = await _mint_box_id(conn, payload.box_number) if _label(payload.box_id) is None else None
    async with conn.transaction():
        await q.lock_box_writes(conn, company, cr_id)
        status = await _assert_boxes_open(conn, tables, header_id, cr_id)
        await assert_base_version(conn, tables, header_id, cr_id, payload.base_version)
        existing = await conn.fetchrow(
            f"SELECT {_BOX_COLS} FROM {tables['boxes']} "
            "WHERE header_id = $1 AND article_description = $2 AND box_number = $3",
            header_id, payload.article_description, payload.box_number,
        )
        _assert_same_label(cr_id, payload.article_description, payload.box_number,
                           sent=_label(payload.box_id),
                           stored=_label(existing["box_id"]) if existing else None,
                           exists=existing is not None)
        if existing:
            box_id = _label(existing["box_id"]) or new_box_id
            await conn.execute(
                f"""
                UPDATE {tables['boxes']} SET
                    box_id = $2,
                    rtv_line_id = COALESCE(rtv_line_id, $3),
                    uom = COALESCE($4, uom),
                    conversion = COALESCE($5, conversion),
                    net_weight = COALESCE($6::numeric, net_weight),
                    gross_weight = COALESCE($7::numeric, gross_weight),
                    lot_number = COALESCE($8, lot_number),
                    item_mark = COALESCE($9, item_mark),
                    spl_remarks = COALESCE($10, spl_remarks),
                    vakkal = COALESCE($11, vakkal),
                    count = COALESCE($12::int, count),
                    updated_at = NOW()
                WHERE id = $1
                """,
                existing["id"], box_id, line_id, payload.uom, payload.conversion,
                payload.net_weight, payload.gross_weight, payload.lot_number,
                payload.item_mark, payload.spl_remarks, payload.vakkal, payload.count,
            )
            result = "updated"
            if _label(existing["box_id"]):          # a reprint: what it changed
                await _log_changes(conn, cr_id, box_id, _changes(existing, payload), actor)
            else:
                await _log_changes(conn, cr_id, box_id, {"printed": (None, box_id)}, actor)
        else:
            box_id = new_box_id
            await conn.execute(
                f"""
                INSERT INTO {tables['boxes']}
                    (header_id, rtv_line_id, article_description, box_number, box_id, uom,
                     conversion, net_weight, gross_weight, lot_number, item_mark,
                     spl_remarks, vakkal, count, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,
                        COALESCE($8::numeric, 0), COALESCE($9::numeric, 0),
                        $10,$11,$12,$13,$14, NOW())
                """,
                header_id, line_id, payload.article_description, payload.box_number, box_id,
                payload.uom, payload.conversion, payload.net_weight, payload.gross_weight,
                payload.lot_number, payload.item_mark, payload.spl_remarks,
                payload.vakkal, payload.count,
            )
            result = "inserted"
            await _log_changes(conn, cr_id, box_id, {"printed": (None, box_id)}, actor)
        await _set_boxed_status(conn, tables, header_id, cr_id)
        await _sync_cold_stock(conn, company, cr_id, status, factory_unit)
        version = await q.box_version(conn, tables, header_id)
    return {"status": result, "box_id": box_id, "rtv_id": cr_id,
            "article_description": payload.article_description, "box_number": payload.box_number,
            "box_version": version}


def by_key(boxes) -> dict:
    """A save's boxes by (article, box_number), the last of a repeated one winning."""
    seen: dict = {}
    for b in boxes:
        seen[(b.article_description, b.box_number)] = b
    return seen


async def read_box_set(conn, tables: dict, header_id: int, cr_id: str, incoming: dict) -> dict:
    """The stored boxes and line ids a save is applied to, read under the box lock (not
    before it) so the insert-vs-update-vs-delete decision can't be made from a stale
    snapshot — _rtv_boxes has no natural-key unique constraint to catch a duplicate
    INSERT, so a check-and-act split across the lock could persist a duplicate
    (header_id, article, box_number) row and orphan a printed box_id. Every label the
    save names is checked against them first: one mismatch refuses the whole save."""
    line_rows = await conn.fetch(
        f"SELECT id, item_description FROM {tables['lines']} WHERE header_id = $1", header_id)
    existing_rows = await conn.fetch(
        f"SELECT {_BOX_COLS} FROM {tables['boxes']} WHERE header_id = $1", header_id)
    stored = {(r["article_description"], r["box_number"]): r for r in existing_rows}
    label_of = {key: _label(r["box_id"]) for key, r in stored.items()}
    for (art, num), b in incoming.items():
        _assert_same_label(cr_id, art, num, sent=_label(b.box_id), stored=label_of.get((art, num)),
                           exists=(art, num) in stored)
    return {"line_id": {r["item_description"]: r["id"] for r in line_rows},
            "stored": stored, "label_of": label_of}


def box_set_changes(incoming: dict, box_set: dict) -> bool:
    """Would writing ``incoming`` change any box: a new one, a changed one, or an
    unprinted one left out (and so removed)."""
    stored = box_set["stored"]
    if any(key not in stored or _changes(stored[key], b) for key, b in incoming.items()):
        return True
    return any(key not in incoming and not box_set["label_of"][key] for key in stored)


async def write_box_set(conn, tables: dict, header_id: int, cr_id: str, incoming: dict,
                        box_set: dict, actor: str) -> dict:
    """Make the stored boxes ``incoming``: insert new, update existing (keeping each
    box_id), delete unprinted ones left out — a printed one left out is kept, never
    deleted. Records what changed on printed boxes, and returns the counts and the
    printed boxes whose label now shows old values (reprint)."""
    stored, label_of, line_id = box_set["stored"], box_set["label_of"], box_set["line_id"]
    inserted = updated = deleted = kept_printed = 0
    reprint: list[dict] = []
    for (art, num), b in incoming.items():
        if (art, num) in stored:
            await conn.execute(
                f"""
                UPDATE {tables['boxes']} SET
                    uom = COALESCE($4, uom),
                    conversion = COALESCE($5, conversion),
                    net_weight = COALESCE($6::numeric, net_weight),
                    gross_weight = COALESCE($7::numeric, gross_weight),
                    lot_number = COALESCE($8, lot_number),
                    item_mark = COALESCE($9, item_mark),
                    spl_remarks = COALESCE($10, spl_remarks),
                    vakkal = COALESCE($11, vakkal),
                    count = COALESCE($12::int, count),
                    rtv_line_id = COALESCE(rtv_line_id, $13),
                    updated_at = NOW()
                WHERE header_id = $1 AND article_description = $2 AND box_number = $3
                """,
                header_id, art, num, b.uom, b.conversion, b.net_weight, b.gross_weight,
                b.lot_number, b.item_mark, b.spl_remarks, b.vakkal, b.count,
                line_id.get(art),
            )
            updated += 1
            label = label_of[(art, num)]
            if label:
                changes = _changes(stored[(art, num)], b)
                await _log_changes(conn, cr_id, label, changes, actor)
                if any(f in changes for f in _LABEL_FIELDS):
                    reprint.append({"article_description": art, "box_number": num, "box_id": label})
        else:
            # box_id stays NULL until the box is actually Printed (upsert_box) —
            # a bulk save is not a print. Minting one here would make the row read
            # back as "printed", freezing it in the UI though no label was produced.
            await conn.execute(
                f"""
                INSERT INTO {tables['boxes']}
                    (header_id, rtv_line_id, article_description, box_number, box_id, uom,
                     conversion, net_weight, gross_weight, lot_number, item_mark, spl_remarks, vakkal,
                     count, created_at)
                VALUES ($1,$2,$3,$4,NULL,$5,$6,
                        COALESCE($7::numeric, 0), COALESCE($8::numeric, 0),
                        $9,$10,$11,$12,$13, NOW())
                """,
                header_id, line_id.get(art), art, num, b.uom, b.conversion,
                b.net_weight, b.gross_weight, b.lot_number, b.item_mark, b.spl_remarks,
                b.vakkal, b.count,
            )
            inserted += 1

    for (art, num) in [key for key in stored if key not in incoming]:
        if label_of[(art, num)]:  # printed: its label is on a carton — keep it
            kept_printed += 1
            continue
        await conn.execute(
            f"DELETE FROM {tables['boxes']} "
            "WHERE header_id = $1 AND article_description = $2 AND box_number = $3",
            header_id, art, num,
        )
        deleted += 1
    return {"inserted": inserted, "updated": updated, "unchanged": 0, "deleted": deleted,
            "kept_printed": kept_printed, "reprint": reprint}


async def bulk_save_boxes(conn, company: str, cr_id: str,
                          data: schemas.CRBulkBoxUpdateRequest,
                          notify_discrepancy: bool = True,
                          allow_clear: bool = False, actor: str = "") -> dict:
    """State-aware full sync of the CR's box set: insert new, update existing
    (preserving box_id), delete unprinted boxes no longer present — a printed one left
    out is kept, never deleted. Every box's label and the page's box-set fingerprint
    are checked first (module docstring). An approved return's status follows
    _set_boxed_status. `notify_discrepancy` is a reserved no-op (kept for signature
    parity). The reply's `reprint` lists the printed boxes whose label shows a value
    this save changed."""
    for b in data.boxes:
        validate.box(b)
    tables = cr_table_names(company)
    header_id, factory_unit = await _resolve_box_writable_header(conn, tables, cr_id)

    if not data.boxes and not allow_clear:
        raise HTTPException(
            400,
            detail={"error": "empty_box_sync",
                    "message": "Refusing to delete all boxes for this return; "
                               "pass allow_clear=true to intentionally clear them.",
                    "details": {"rtv_id": cr_id}},
        )

    incoming = by_key(data.boxes)
    async with conn.transaction():
        await q.lock_box_writes(conn, company, cr_id)
        status = await _assert_boxes_open(conn, tables, header_id, cr_id)
        await assert_base_version(conn, tables, header_id, cr_id, data.base_version)
        box_set = await read_box_set(conn, tables, header_id, cr_id, incoming)
        counts = await write_box_set(conn, tables, header_id, cr_id, incoming, box_set, actor)
        await _set_boxed_status(conn, tables, header_id, cr_id)
        await _sync_cold_stock(conn, company, cr_id, status, factory_unit)
        version = await q.box_version(conn, tables, header_id)

    return {"status": "synced", "rtv_id": cr_id, **counts, "box_version": version}


async def log_box_edits(conn, payload: schemas.CRBoxEditLogRequest, email_id: str) -> dict:
    """Append one audit row per change to the global box_edit_logs table.
    `email_id` is the JWT actor (payload.email_id is ignored — hardening).

    Only for a box of the return it names, and only for a box field. box_edit_logs is
    shared: Material In reads its 'deleted' rows to number a purchase order's boxes, so
    an entry for any box id under any transaction could stop that PO's box saves, and
    a made-up entry would show as an edit in the export. The box writes record their
    own changes now (_log_changes); this stays for older screens."""
    bad = sorted({ch.field_name for ch in payload.changes} - set(_TRACKED))
    if bad:
        raise validate.refuse("unknown_box_field", f"{', '.join(bad)} is not a box field.",
                              fields=bad)
    owned = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM cfpl_rtv_boxes b JOIN cfpl_rtv_header h ON h.id = b.header_id "
        "WHERE h.rtv_id = $1 AND b.box_id = $2) OR EXISTS (SELECT 1 FROM cdpl_rtv_boxes b "
        "JOIN cdpl_rtv_header h ON h.id = b.header_id WHERE h.rtv_id = $1 AND b.box_id = $2)",
        payload.rtv_id, payload.box_id)
    if not owned:
        raise HTTPException(
            404, detail={"error": "box_not_on_return",
                         "message": f"Box {payload.box_id} is not on customer return {payload.rtv_id}.",
                         "details": {"rtv_id": payload.rtv_id, "box_id": payload.box_id}})
    edited_at = datetime.now(timezone.utc)  # one shared timestamp per call
    async with conn.transaction():
        for ch in payload.changes:
            description = f"Changed {ch.field_name} from '{ch.old_value}' to '{ch.new_value}'"
            await conn.execute(
                """
                INSERT INTO box_edit_logs
                    (email_id, description, transaction_no, box_id, field_name,
                     old_value, new_value, edited_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                """,
                email_id, description, payload.rtv_id, payload.box_id,
                ch.field_name, ch.old_value, ch.new_value, edited_at,
            )
    return {"status": "logged", "entries": len(payload.changes)}
