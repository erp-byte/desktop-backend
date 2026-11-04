"""Scan & send saves on scan (doc 07): each box scanned for an Approved request is saved
as it is scanned, and the screen changes the transfer in place.

  POST  /requests/{id}/scan            scan_box            first scan creates the request's
                                                           transfer; each later one adds to it
  POST  /transfers/{id}/remove-boxes   remove_boxes        removing the last box cancels it
  PATCH /transfers/{id}/boxes          update_box_weights
  PATCH /transfers/{id}/transport      update_transport

The first scan is create_transfer with that one box (create_service._create): the request
must be Approved and becomes Transferred; route, lines and reason are the request's; the
box is judged R1-R3 as create judges a dispatch's boxes. Vehicle number, driver name and
approval authority must be filled in first (409). Each later scan adds its box to the
request's transfer (its newest: query_service.newest_transfers) by the same rules, judged
with the boxes the transfer already has: only boxes the request asks for, each by its own
record (R1/R2, create_service._refuse_boxes_off_the_request); a box that can go from the
From site (R3, _refuse_unavailable_boxes); not one already on it, nor one whose box id a
box of another transaction on it already has (409: a receipt takes one box per box id).
A later scan keeps the transport details the transfer has (update_transport changes them).

Each save is one transaction that locks the request row first (FOR UPDATE): one request's
scans, removals and edits run one after another, so two scans cannot both pass the rules
for the same quantity or both create a transfer; the Edit page and a delete of the
transfer take the same lock (create_service._LOCK_REQUEST_OF). The user must be able to
dispatch from the request's stored From site (permissions.assert_can_dispatch_from),
whatever the client sends. Only a transfer from a request is changed here (the Edit page
changes the others), and only while it is in transit — Dispatch / Partial, no receipt
(GRN, or IMS's cold receipt) yet — else 409 (create_service.refuse_received, which the
Edit page's save applies too). That is checked with the transfer's header row locked (FOR
UPDATE), which a receipt takes before it begins (receive_service.create_pending_transfer_in),
so a receipt and a change here never cross. A request or transfer stored without a From
or a To site, or from and to one site in two spellings ('A185' -> 'Warehouse A185'),
which older code accepted, is refused (409), as the dispatch header refuses such a route;
so is a weight edit of a box that has gone on a newer transfer since it joined this one
(its receipt here undone since), which is that transfer's box now.

After every save the transfer's rows (header status / from_cold_unit / unallocated_boxes,
lines, box rows, in-transit rows) are what create_transfer would have written for the
same request with the same boxes in the same order: a box row and its in-transit copy are
written as create writes them (create_service._insert_box, reversal_service.park_boxes), a
weight edit re-parks its box from its own copy (its stock snapshot is kept; _reweighed),
and every save ends with create_service._settle. Removing the last box deletes the transfer as
delete_transfer does and puts the request back to Approved. Record only: the stock tables
(and ERP Material In's po_box) are read, never written.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException

from app.core import sites
from app.modules.auth.middleware import AuthUser
from app.modules.transfer import permissions, schemas
from app.modules.transfer.services import (
    create_service,
    delete_service,
    lookup_service,
    material_in_source,
    query_service,
    request_notify,
    reversal_service,
)
from app.modules.transfer.services.stock_service import _dec, _table_exists

FILL_TRANSPORT = "Fill in vehicle number, driver name and approval authority before scanning."
USE_EDIT = "Only a transfer from a request is changed here; use Edit."

# The transfer a save changes, its header row locked (after its request row): a receipt
# of it begins under the same lock, so the not-received check (_refuse_received) holds
# until the save commits.
_TRANSFER = (
    "SELECT id, challan_no, status, request_id, stock_trf_date, from_site, to_site, remark, "
    "vehicle_no, driver_name, approved_by, created_by FROM interunit_transfers_header WHERE id = $1 "
    "FOR UPDATE"
)
# The stock tables an in-transit copy names as its source (reversal_service), read by id.
_STOCK_TABLES = (*reversal_service._COLD_TABLES, *reversal_service._WH_TABLES)
# One box row of a transfer, matched trimmed, as the client and the pending rows name it.
_BOX = (
    "SELECT box_number, box_id, transaction_no, article, lot_number, batch_number, net_weight, "
    "gross_weight FROM interunit_transfer_boxes "
    "WHERE header_id = $1 AND btrim(box_id) = $2 AND btrim(transaction_no) = $3"
)
_TRANSPORT = (("vehicle_no", "vehicle number"), ("driver_name", "driver name"),
              ("approved_by", "approval authority"))


def _key(box_id, transaction_no) -> tuple[str, str]:
    """A box as the client names it: (box_id, transaction_no), trimmed."""
    return (box_id or "").strip(), (transaction_no or "").strip()


def _refuse_one_site(what: str, from_site, to_site, tail: str) -> None:
    """409 for a stored route the dispatch header (schemas.TransferHeaderCreate) refuses
    and older code accepted: one without a From or a To site (blank, or no letter or digit
    in it), or one site twice ('A185' -> 'Warehouse A185'). The scan screen says so plainly
    instead of failing on the header it builds from the route."""
    missing = [leg for leg, site in (("From", from_site), ("To", to_site)) if not sites.site_key(site)]
    if missing:
        raise HTTPException(409, f"{what} has no {' or '.join(missing)} site; {tail}")
    if sites.same_site(from_site, to_site):
        raise HTTPException(
            409, f"{what} is from and to the same site ({sites.canonical_site(from_site)}); {tail}")


def _box_out(r) -> dict:
    """A stored box row as ScannedBox."""
    return {
        "box_number": r["box_number"] or 0, "box_id": r["box_id"] or "",
        "transaction_no": r["transaction_no"] or "", "article": r["article"] or "",
        "lot_number": r["lot_number"], "batch_number": r["batch_number"],
        "net_weight": query_service._s(r["net_weight"]), "gross_weight": query_service._s(r["gross_weight"]),
    }


async def _summary(conn, transfer_id: int) -> dict:
    """The transfer as a save returns it: id, challan, status and its real boxes (box_id +
    transaction_no, not 'DIRECT')."""
    h = await conn.fetchrow(
        "SELECT challan_no, status FROM interunit_transfers_header WHERE id = $1", transfer_id)
    rows = await conn.fetch(
        "SELECT box_id, transaction_no FROM interunit_transfer_boxes WHERE header_id = $1", transfer_id)
    return {"transfer_id": transfer_id, "challan_no": h["challan_no"] or "", "status": h["status"] or "",
            "box_count": sum(1 for r in rows if lookup_service._box_key(r["box_id"], r["transaction_no"]))}


async def _refuse_received(conn, t: dict) -> None:
    """409 once the transfer is no longer in transit: a status other than an in-transit
    one, or any receipt of it in either ledger, a pending one too
    (create_service.refuse_received, the rule the Edit page's save applies too)."""
    await create_service.refuse_received(conn, t, "it can't be changed here.")


async def _open(conn, transfer_id: int, user: AuthUser) -> dict:
    """The transfer a change on the scan screen is made to, inside the caller's
    transaction, with its request row locked first (create_service._LOCK_REQUEST_OF),
    then its own (_TRANSFER). 404 without the transfer; 403 unless the user may dispatch
    from its stored From site; 409 for one without a request (USE_EDIT) or whose request
    has since been deleted, and for one that has been received (_refuse_received)."""
    req = await conn.fetchrow(create_service._LOCK_REQUEST_OF, transfer_id)
    t = await conn.fetchrow(_TRANSFER, transfer_id)
    if not t:
        raise HTTPException(404, "Transfer not found")
    t = dict(t)
    permissions.assert_can_dispatch_from(user, t["from_site"])
    if t["request_id"] is None:
        raise HTTPException(409, USE_EDIT)
    if not req:
        raise HTTPException(409, f"The request of transfer {t['challan_no']} no longer exists; use Edit.")
    await _refuse_received(conn, t)
    return t


# ── Scan ──────────────────────────────────────────────────────────────────────
async def scan_box(conn, request_id: int, body: schemas.ScanBox, user: AuthUser) -> dict:
    """Save one scanned box of a request (POST /requests/{id}/scan) and return the
    transfer (its real box count, Dispatch / Partial) with the box as stored.

    With the request row locked, its newest transfer decides: none and the request
    Approved — the box creates it (_first_box); one and the request Transferred — the box
    is added to it (_add_box). Otherwise 409: a request not Approved as create refuses it;
    a Transferred one without a transfer, or an Approved one that has one, by name."""
    box = body.box
    key = lookup_service._box_key(box.box_id, box.transaction_no)
    if key is None:
        raise HTTPException(422, "A scanned box needs its box id and transaction number.")
    async with conn.transaction():
        req = await conn.fetchrow(
            "SELECT id, request_no, status, from_site, to_site FROM interunit_transfer_requests "
            "WHERE id = $1 FOR UPDATE",
            request_id)
        if not req:
            raise HTTPException(404, "Request not found")
        permissions.assert_can_dispatch_from(user, req["from_site"])
        request_no = req["request_no"] or str(request_id)
        transfer = (await query_service.newest_transfers(conn, [request_id])).get(request_id)
        if req["status"] == "Transferred":
            if transfer is None:
                raise HTTPException(409, f"Request {request_no} has no transfer to add boxes to.")
            transfer_id, created = await _add_box(conn, transfer["id"], box, key), False
        elif req["status"] != "Approved":
            raise HTTPException(409, create_service._not_approved(req["status"]))
        elif transfer is not None:
            raise HTTPException(
                409, f"Request {request_no} already has transfer {transfer['challan_no']}; "
                     "it can't be dispatched again.")
        else:
            transfer_id, created = await _first_box(conn, req, body.header, box, user), True
        out = await _summary(conn, transfer_id)
        stored = await conn.fetchrow(_BOX, transfer_id, *key)
    if created:
        # Committed: the request is Transferred - tell its requester (background, best-effort).
        request_notify.notify("dispatched", request_id)
    return {**out, "created": created, "box": _box_out(stored)}


async def _first_box(conn, req, h: schemas.ScanHeader, box: schemas.BoxCreate, user: AuthUser) -> int:
    """The first scan of an Approved request: once its transport is filled in (409
    FILL_TRANSPORT), create its transfer with this one box exactly as create_transfer
    does (create_service._create: the request's route, lines and reason; R1-R3; the
    request marked Transferred). Returns the transfer's id."""
    if not all((v or "").strip() for v in (h.vehicle_no, h.driver_name, h.approved_by)):
        raise HTTPException(409, FILL_TRANSPORT)
    _refuse_one_site(f"Request {req['request_no'] or req['id']}", req["from_site"], req["to_site"],
                     "it can't be dispatched.")
    header = schemas.TransferHeaderCreate(
        challan_no=(h.challan_no or "").strip() or None, stock_trf_date=h.stock_trf_date or "",
        from_warehouse=req["from_site"] or "", to_warehouse=req["to_site"] or "",
        vehicle_no=h.vehicle_no.strip(), driver_name=h.driver_name.strip(), approved_by=h.approved_by.strip(),
    )
    return await create_service._create(conn, header, [], [box], req["id"], user.email or "unknown")


async def _add_box(conn, transfer_id: int, box: schemas.BoxCreate, key: tuple[str, str]) -> int:
    """A later scan: add the box to the request's transfer (read with its header row
    locked, _TRANSFER; not received; not a box it has, nor one whose box id a box of
    another transaction on it has, create_service._shared_box_id), judged as create judges
    a dispatch's boxes, together with the boxes it already has — the new one last, so it
    is the one refused — then store it against its line, park it as create parks a box
    and settle. Returns the transfer's id."""
    t = dict(await conn.fetchrow(_TRANSFER, transfer_id))
    await _refuse_received(conn, t)
    rows = sorted(await conn.fetch(
        "SELECT id, box_number, box_id, transaction_no, article, lot_number, batch_number "
        "FROM interunit_transfer_boxes WHERE header_id = $1",
        t["id"]), key=lambda r: r["id"])
    held = [k for k in (lookup_service._box_key(r["box_id"], r["transaction_no"]) for r in rows) if k]
    if key in held:
        raise HTTPException(409, f"Box {key[0]} is already on transfer {t['challan_no']}.")
    twin = next((k for k in held if k[0] == key[0]), None)
    if twin is not None:
        raise HTTPException(409, create_service._shared_box_id(key[0], twin[1], key[1], t["challan_no"]))

    # R2 / R1: the request's terms (its route is the transfer's); its boxes as saved, this
    # one as the lookups find it in stock. R3: this box, from the request's From site.
    _refuse_one_site(f"Transfer {t['challan_no']}", t["from_site"], t["to_site"], "no box can be added to it.")
    route = schemas.TransferHeaderCreate(stock_trf_date="", from_warehouse=t["from_site"] or "",
                                         to_warehouse=t["to_site"] or "", vehicle_no=t["vehicle_no"] or "")
    terms = await create_service._dispatch_terms(conn, t["request_id"], route, [], saved=t)
    held = [schemas.BoxCreate(box_number=r["box_number"] or 0, box_id=r["box_id"], article=r["article"] or "",
                              lot_number=r["lot_number"], batch_number=r["batch_number"],
                              transaction_no=r["transaction_no"]) for r in rows]
    new = (await create_service._refuse_boxes_off_the_request(conn, terms, [*held, box], t["id"]))[-1]
    await create_service._refuse_unavailable_boxes(conn, [new], terms.from_site, transfer_id=t["id"])

    lines = sorted(await conn.fetch(
        "SELECT id, item_desc_raw FROM interunit_transfers_lines WHERE header_id = $1",
        t["id"]), key=lambda l: l["id"])
    parked = await create_service._insert_box(conn, t["id"], lines, new)
    await reversal_service.park_boxes(
        conn, transfer_out_id=t["id"], challan_no=t["challan_no"],
        from_site=t["from_site"], to_site=t["to_site"], boxes=[parked],
        dispatched_at=create_service._dispatched_at(t["stock_trf_date"]),
        dispatched_by=t["created_by"] or "system", cold_fallback=True, take_over=True,
    )
    await create_service._settle(conn, t["id"])
    return t["id"]


# ── Remove ────────────────────────────────────────────────────────────────────
async def remove_boxes(conn, transfer_id: int, body: schemas.RemoveBoxes, user: AuthUser) -> dict:
    """Take boxes off a request's transfer (POST /transfers/{id}/remove-boxes): the named
    boxes (404 naming any it does not have), or all of them. Their box rows and in-transit
    rows go and the transfer is settled. Once no real box is left the transfer goes, as
    delete_transfer deletes it, and its request is Approved again (updated_at now; the
    decisions as they were), ready to be sent anew."""
    remove_all = body.all is True
    if remove_all == bool(body.boxes):
        raise HTTPException(422, "Name the boxes to remove, or send all: true (one of the two).")
    async with conn.transaction():
        t = await _open(conn, transfer_id, user)
        on_it = [_key(r["box_id"], r["transaction_no"]) for r in await conn.fetch(
            "SELECT box_id, transaction_no FROM interunit_transfer_boxes WHERE header_id = $1", transfer_id)]
        if remove_all:
            gone = list(dict.fromkeys(on_it))
        else:
            gone = list(dict.fromkeys(_key(b.box_id, b.transaction_no) for b in body.boxes))
            missing = [bid for bid, tno in gone if (bid, tno) not in on_it]
            if missing:
                raise HTTPException(404, create_service._refusal(
                    [f"Box {bid} is not on transfer {t['challan_no']}." for bid in missing]))

        if not any(lookup_service._box_key(*k) for k in on_it if k not in gone):
            await delete_service._delete_transfer_rows(conn, transfer_id)
            await conn.execute(
                "UPDATE interunit_transfer_requests SET status = 'Approved', updated_at = $1 WHERE id = $2",
                datetime.now(), t["request_id"])
            return {"cancelled": True, "request_status": "Approved"}

        for bid, tno in gone:
            await conn.execute(
                "DELETE FROM interunit_transfer_boxes "
                "WHERE header_id = $1 AND btrim(box_id) = $2 AND btrim(transaction_no) = $3",
                transfer_id, bid, tno)
            await conn.execute(
                "DELETE FROM pending_transfer_stock "
                "WHERE transfer_out_id = $1 AND btrim(box_id) = $2 AND btrim(transaction_no) = $3",
                transfer_id, bid, tno)
        await create_service._settle(conn, transfer_id)
        out = await _summary(conn, transfer_id)
    return {"cancelled": False, "transfer_id": transfer_id, "status": out["status"], "box_count": out["box_count"]}


# ── Weights ───────────────────────────────────────────────────────────────────
async def _reweighed(conn, own: dict, net: float) -> dict:
    """A box's in-transit copy `own` as park_boxes takes it back (`prior`) after a weight
    edit, so the box parks as create would park it with its new weights. park_boxes gives
    a box its stock row's own weight when the row has one, else the box's — weight_kg of a
    cold copy, net_weight of a warehouse one (whose gross is the stock row's alone) — and
    the copy holds whichever it was. The stock row behind it (source_table, source_row_id)
    is read only to tell which: a row with a weight of its own keeps the copy's (the
    snapshot); a row without one gives the box's new weight (`net`). A copy whose stock
    row has gone is kept as it is. A Material In copy (source_table
    material_in_source.SOURCE_TABLE, no source_row_id) is a warehouse copy, told by its
    po_box row (material_in_source.snapshot) too; but Material In stays editable after a
    dispatch, so the copy holds the row's weight only while the two agree: a row without a
    net weight, or with another one (weighed or corrected in Material In since), gives the
    box's new weight. Read only."""
    table, row_id = own.get("source_table"), own.get("source_row_id")
    if table == material_in_source.SOURCE_TABLE:
        stock = await material_in_source.snapshot(conn, own.get("box_id"), own.get("transaction_no"))
        if stock is None:
            return own
        held = stock.get("net_weight") and _dec(stock["net_weight"]) == _dec(own.get("net_weight"))
        return own if held else {**own, "net_weight": net}
    if table not in _STOCK_TABLES or row_id is None or not await _table_exists(conn, table):
        return own
    stock = await conn.fetchrow(f"SELECT * FROM {table} WHERE id = $1", row_id)
    if stock is None:
        return own
    if own.get("cold_storage_data") is not None:
        return own if stock.get("weight_kg") else {**own, "weight_kg": None}
    return own if stock.get("net_weight") else {**own, "net_weight": net}


async def _refuse_sent_on(conn, t: dict, key: tuple[str, str]) -> None:
    """409 when the box has gone on a newer transfer since it joined this one
    (lookup_service.sent_on_since): this transfer's receipt was undone after the box was
    sent on from its To site, so the box is that transfer's now. Changing it here would
    park it back in transit on this one and take it off the transfer it is on (the
    weight edit's take_over). Names the transfer the box is on (its latest)."""
    if not await lookup_service.sent_on_since(conn, [key], t["id"]):
        return
    on = await lookup_service.unavailable_boxes(conn, [key], transfer_id=t["id"])
    challan = on[0].get("challan_no") if on else None
    raise HTTPException(
        409, f"Box {key[0]} has since been sent on {f'challan {challan}' if challan else 'another transfer'}, "
             f"so its weights on transfer {t['challan_no']} can't be changed.")


async def update_box_weights(conn, transfer_id: int, body: schemas.BoxWeights, user: AuthUser) -> dict:
    """Change a box's net / gross weight on a request's transfer (PATCH
    /transfers/{id}/boxes): 422 for a negative weight, or net above gross when gross is
    set; 404 for a box it does not have. The box row takes the weights, and its in-transit
    row is parked again as create would park it with them, from its own copy (`prior`,
    _reweighed): the stock snapshot it was dispatched with stays, whatever the stock row
    is now; only a weight that was the box's own takes the new one. Returns the box as
    stored."""
    key = _key(body.box_id, body.transaction_no)
    net, gross = body.net_weight, body.gross_weight
    if net < 0 or gross < 0:
        raise HTTPException(422, f"Box {key[0]}: a weight can't be negative")
    if gross > 0 and net > gross:
        raise HTTPException(422, f"Box {key[0]}: net weight exceeds gross")
    async with conn.transaction():
        t = await _open(conn, transfer_id, user)
        if not await conn.fetchrow(_BOX, transfer_id, *key):
            raise HTTPException(404, f"Box {key[0]} is not on transfer {t['challan_no']}.")
        await _refuse_sent_on(conn, t, key)
        await conn.execute(
            "UPDATE interunit_transfer_boxes SET net_weight = $1, gross_weight = $2 "
            "WHERE header_id = $3 AND btrim(box_id) = $4 AND btrim(transaction_no) = $5",
            _dec(net), _dec(gross), transfer_id, *key)
        box = {**dict(await conn.fetchrow(_BOX, transfer_id, *key)), "box_id": key[0], "transaction_no": key[1]}
        own = await conn.fetchrow(
            "SELECT * FROM pending_transfer_stock "
            "WHERE transfer_out_id = $1 AND btrim(box_id) = $2 AND btrim(transaction_no) = $3",
            transfer_id, *key)
        prior = None
        if own is not None:
            await conn.execute("DELETE FROM pending_transfer_stock WHERE id = $1", own["id"])
            # The lot its copy was parked with (the box row stores none as '').
            box["lot_number"] = own["lot_no"]
            prior = {key: await _reweighed(conn, dict(own), net)}
        await reversal_service.park_boxes(
            conn, transfer_out_id=transfer_id, challan_no=t["challan_no"],
            from_site=t["from_site"], to_site=t["to_site"], boxes=[box],
            dispatched_at=create_service._dispatched_at(t["stock_trf_date"]),
            dispatched_by=t["created_by"] or "system", cold_fallback=True, prior=prior, take_over=True,
        )
        await create_service._settle(conn, transfer_id)
        stored = await conn.fetchrow(_BOX, transfer_id, *key)
    return _box_out(stored)


# ── Transport ─────────────────────────────────────────────────────────────────
async def update_transport(conn, transfer_id: int, body: schemas.TransportUpdate, user: AuthUser) -> dict:
    """Change a request's transfer's vehicle number, driver name and approval authority
    (PATCH /transfers/{id}/transport), each required (422 naming the blank ones). Those
    three header columns only."""
    values = {field: (getattr(body, field) or "").strip() for field, _ in _TRANSPORT}
    blank = [label for field, label in _TRANSPORT if not values[field]]
    if blank:
        names = blank[0] if len(blank) == 1 else f"{', '.join(blank[:-1])} and {blank[-1]}"
        raise HTTPException(422, f"Fill in {names}.")
    async with conn.transaction():
        await _open(conn, transfer_id, user)
        await conn.execute(
            "UPDATE interunit_transfers_header SET vehicle_no = $1, driver_name = $2, approved_by = $3 "
            "WHERE id = $4",
            values["vehicle_no"], values["driver_name"], values["approved_by"], transfer_id)
    return {"transfer_id": transfer_id, **values}
