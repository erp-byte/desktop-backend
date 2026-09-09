"""Job Work Material Out — create (POST /api/v1/job-work/out).

Ported from job_work_server.submit_material_out + _deduct_cold_storage_stock.

The endpoint does two things, and the second is the one that matters: it writes
a challan, and it DELETES the dispatched boxes out of cfpl/cdpl_cold_stocks.
Those are the live inventory tables IMS reads. Divergences from the reference,
all of them deliberate:

  • ONE TRANSACTION. The reference commits the header, the lines and the cold
    deduction as one SQLAlchemy session too, but nothing pins the deduction to
    the challan — here a failure anywhere rolls back both, so inventory can
    never be deducted for a challan that does not exist (or vice versa).
  • DUPLICATE CHALLAN REJECTED (409). The reference has no uniqueness guard at
    any level, so a double-submit wrote two challans AND deducted the same boxes
    twice — the second deduction silently finding nothing and reporting success.
  • DUPLICATE BOX WITHIN A CHALLAN REJECTED (422). Two lines carrying the same
    (box_id, transaction_no) deducted ONE cold row but dispatched two lines'
    worth of stock. Same class of bug the transfer module's box guard exists for.
  • SNAPSHOT KEYED BY LINE id, not by (header_id, box_id, transaction_no) as the
    reference does — that WHERE clause stamped every line sharing a box.
  • DEDUCTION COUNT RETURNED. The caller could not otherwise distinguish a
    dispatch that removed 40 boxes from one that removed none because the box
    ids were wrong.

Actor comes from the JWT, never from a `?created_by=` query param.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import HTTPException

from app.modules.job_work import schemas
from app.modules.job_work.services import query_service as q
from app.modules.job_work.tables import company_of, resolve_cold_table

logger = logging.getLogger(__name__)

DISPOSITION_TYPE = "job_work_out"


def _s(v: Any) -> str:
    """-> str for the VARCHAR weight/pack columns. None becomes '' (not 'None')."""
    return "" if v is None else str(v)


async def _table_exists(conn, table: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1)", f"public.{table}"))


async def _cached_exists(conn, cache: dict, table: str) -> bool:
    """_table_exists memoised into the caller's per-challan dict, so a 200-line challan
    probes each table once rather than once per line."""
    if table not in cache:
        cache[table] = await _table_exists(conn, table)
    return cache[table]


# ── Guards ───────────────────────────────────────────────────────────────────
async def _assert_challan_unused(conn, challan_no: str) -> None:
    """409 if the challan already exists.

    Not a DB constraint: live data predates this endpoint and may already hold
    duplicates, so a UNIQUE index could not be added (see 088_job_work.sql).
    Two truly concurrent inserts could still both pass under READ COMMITTED —
    but the realistic case is a double-clicked submit re-sending one challan_no,
    and that is caught.
    """
    existing = await conn.fetchval(
        "SELECT id FROM jb_materialout_header WHERE challan_no = $1 LIMIT 1", challan_no)
    if existing:
        raise HTTPException(
            409,
            detail={
                "error": "challan_already_exists",
                "message": f"Challan {challan_no} has already been submitted",
                "details": {"challan_no": challan_no, "id": existing},
            },
        )


def _assert_boxes_unique(lines: list[schemas.JobWorkLineCreate]) -> None:
    """422 if one scanned box appears on two lines of the same challan.

    Only ONE cold_stocks row exists per (box_id, transaction_no), so the second
    line would dispatch stock that was never deducted.
    """
    seen: dict[tuple[str, str], int] = {}
    for idx, line in enumerate(lines):
        if not (line.box_id and line.transaction_no):
            continue
        key = (line.box_id, line.transaction_no)
        if key in seen:
            raise HTTPException(
                422,
                detail={
                    "error": "duplicate_box",
                    "message": (f"Box {line.box_id} ({line.transaction_no}) is on "
                                f"line {seen[key] + 1} and line {idx + 1}"),
                    "details": {"box_id": line.box_id,
                                "transaction_no": line.transaction_no,
                                "line_indexes": [seen[key], idx]},
                },
            )
        seen[key] = idx


# ── Writes ───────────────────────────────────────────────────────────────────
async def _insert_header(conn, data: schemas.JobWorkCreateRequest,
                         raw_payload: dict, created_by: str) -> int:
    h, party = data.header, data.dispatch_to
    # `type` and `status` are literals, exactly as the reference writes them:
    # this endpoint can only ever create a dispatched OUT challan.
    row = await conn.fetchrow(
        """
        INSERT INTO jb_materialout_header
            (challan_no, job_work_date, from_warehouse, to_party, party_address,
             party_state, party_city, party_pin_code, party_contact_company,
             party_contact_mobile, party_email, sub_category,
             contact_person, contact_number, purpose_of_work, expected_return_date,
             vehicle_no, driver_name, authorized_person, remarks,
             e_way_bill_no, dispatched_through, type, status, dispatch_to, payload, created_by)
        VALUES
            ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
             $21,$22,'OUT','sent',$23::jsonb,$24::jsonb,$25)
        RETURNING id
        """,
        h.challan_no, h.job_work_date, h.from_warehouse, h.to_party, h.party_address,
        party.state, party.city, party.pin_code, party.contact_company,
        party.contact_mobile, party.email, party.sub_category,
        h.contact_person, h.contact_number, h.purpose_of_work, h.expected_return_date,
        h.vehicle_no, h.driver_name, h.authorized_person, h.remarks,
        h.e_way_bill_no, h.dispatched_through,
        json.dumps(party.model_dump()),
        # The RAW body, not the parsed model: `company`, `totals`, `tax_summary`,
        # per-line `hsn_sac` / `gst_rate` / `unit_pack_size` have no columns and
        # this blob is the only place they survive.
        json.dumps(raw_payload, default=str),
        created_by,
    )
    return int(row["id"])


async def _insert_line(conn, header_id: int, line: schemas.JobWorkLineCreate) -> int:
    """Insert one dispatched article; returns its id.

    item_category falls back to the all_sku master when the client leaves it
    blank — the same correlated sub-select the reference uses, matched on
    UPPER(TRIM(particulars)). A description that does not match any master row
    yields NULL rather than an error.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO jb_materialout_lines
            (header_id, sl_no, item_description, material_type, item_category, sub_category,
             quantity_kgs, quantity_boxes, rate_per_kg, amount,
             uom, case_pack, net_weight, total_weight,
             batch_number, lot_number, manufacturing_date, expiry_date, line_remarks,
             box_id, transaction_no, cold_unit, item_mark)
        VALUES
            ($1,$2,$3,$4,
             COALESCE(NULLIF($5, ''), (SELECT UPPER(item_group) FROM all_sku
                 WHERE UPPER(TRIM(particulars)) = UPPER(TRIM($3))
                   AND item_group IS NOT NULL AND item_group <> '' LIMIT 1)),
             $6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23)
        RETURNING id
        """,
        header_id, line.sl_no, line.item_description, line.material_type,
        line.item_category, line.sub_category,
        line.quantity_kgs, line.quantity_boxes, line.rate_per_kg, line.amount,
        line.uom, _s(line.case_pack), _s(line.net_weight), _s(line.total_weight),
        line.batch_number, line.lot_number, line.manufacturing_date, line.expiry_date,
        line.line_remarks, line.box_id, line.transaction_no, line.cold_unit, line.item_mark,
    )
    return int(row["id"])


# ── Cold-storage deduction (the destructive part) ────────────────────────────
async def _write_disposition(conn, *, box_id: str, transaction_no: str,
                             cold_row: Optional[dict], line: schemas.JobWorkLineCreate,
                             source_table: str, header_id: int, challan_no: str,
                             disposed_by: str) -> None:
    """Append to cold_stock_disposition: the append-only ledger of why a box left
    inventory. STBR reads it during a later Transfer-In scan to recognise a
    "missing" box as a legitimate job-work dispatch rather than a lost box.

    The table is created by IMS, not by this repo's migrations, so it may be
    absent. The write runs in its OWN SAVEPOINT: without it, a failed statement
    would abort the enclosing transaction and take the whole challan down with
    it — audit is not worth losing the dispatch over.
    """
    row = dict(cold_row or {})
    row.pop("id", None)
    try:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO cold_stock_disposition
                    (box_id, transaction_no, lot_no, item_description,
                     from_company, unit, from_site, source_table,
                     disposition_type, disposition_ref_table, disposition_ref_id,
                     disposition_ref_no, disposed_by, snapshot_data, notes)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'jb_materialout_header',$10,$11,$12,
                        $13::jsonb,'Job Work material-out')
                """,
                box_id, transaction_no,
                row.get("lot_no") or line.lot_number or None,
                row.get("item_description") or line.item_description or None,
                company_of(source_table),
                row.get("unit") or line.cold_unit or None,
                row.get("storage_location") or row.get("warehouse") or None,
                source_table, DISPOSITION_TYPE, header_id, challan_no, disposed_by,
                json.dumps(row, default=str) if row else None,
            )
    except Exception as e:  # noqa: BLE001 — audit is best-effort by design
        logger.warning("cold_stock_disposition write failed for box %s (%s): %s",
                       box_id, transaction_no, e)


async def _deduct_cold_storage(conn, *, header_id: int, challan_no: str, disposed_by: str,
                               lines: list[schemas.JobWorkLineCreate],
                               line_ids: list[int]) -> int:
    """Snapshot then DELETE each dispatched box from its cold_stocks table.

    A line is only deducted when box_id, transaction_no AND cold_unit are all
    present — a partially-filled line is skipped, matching the reference. That
    is worth knowing: such a line dispatches stock on paper without removing it
    from inventory, which is why the return value is surfaced to the caller.

    Returns the number of cold rows actually removed.
    """
    deducted = 0
    checked: dict[str, bool] = {}

    for line, line_id in zip(lines, line_ids):
        if not (line.box_id and line.transaction_no and line.cold_unit):
            continue
        table = resolve_cold_table(line.cold_unit)
        if not table:
            logger.warning("Challan %s line %s: unrecognised cold_unit %r — not deducted",
                           challan_no, line.sl_no, line.cold_unit)
            continue
        if table not in checked:
            checked[table] = await _table_exists(conn, table)
        if not checked[table]:
            # Dev/CI DBs carry the challan tables but not the warehouse inventory
            # tables. Skip rather than fail — same posture as transfer/park_boxes.
            logger.warning("Cold table %s absent — challan %s not deducted", table, challan_no)
            continue

        # A box already dispatched on an interunit transfer is on a truck, not in the
        # cold room. Its cold_stocks row used to be DELETED at dispatch, so the
        # `cold_row is None` branch below was what kept job work off it. The transfer park
        # is non-destructive now (reversal_service.park_boxes writes a
        # cold_stock_disposition row instead), so the row survives and that branch no
        # longer fires — without this check job work would delete the cold row of a box
        # that is in transit and report it as a genuine deduction.
        in_transit = await conn.fetchval(
            "SELECT transfer_out_challan_no FROM pending_transfer_stock "
            "WHERE box_id = $1 AND transaction_no = $2 AND status = 'In Transit' LIMIT 1",
            line.box_id, line.transaction_no,
        ) if await _cached_exists(conn, checked, "pending_transfer_stock") else None
        if in_transit is not None:
            raise HTTPException(
                status_code=409,
                detail=(f"Box {line.box_id} ({line.transaction_no}) is in transit on transfer "
                        f"challan {in_transit or '—'} and cannot be sent to job work. "
                        "Receive or delete that transfer first."),
            )

        cold_row = await conn.fetchrow(
            f"SELECT * FROM {table} WHERE box_id = $1 AND transaction_no = $2",
            line.box_id, line.transaction_no,
        )

        if cold_row is None:
            # Already gone (moved, consumed, or the ids are wrong). Keep the
            # client's copy as the only surviving description of the box, but do
            # NOT count it as deducted — nothing left inventory.
            if line.cold_stock_snapshot:
                await conn.execute(
                    "UPDATE jb_materialout_lines SET cold_storage_snapshot = $1::jsonb "
                    "WHERE id = $2",
                    json.dumps({k: (str(v) if v is not None else None)
                                for k, v in line.cold_stock_snapshot.items() if k != "id"},
                               default=str),
                    line_id,
                )
            logger.warning("Challan %s: box %s (%s) not in %s — dispatched but not deducted",
                           challan_no, line.box_id, line.transaction_no, table)
            continue

        snapshot = {k: (str(v) if v is not None else None)
                    for k, v in dict(cold_row).items() if k != "id"}
        # Client snapshot only fills gaps; the live row always wins.
        for k, v in (line.cold_stock_snapshot or {}).items():
            if k != "id" and snapshot.get(k) is None and v is not None:
                snapshot[k] = str(v)

        await conn.execute(
            "UPDATE jb_materialout_lines SET cold_storage_snapshot = $1::jsonb WHERE id = $2",
            json.dumps(snapshot, default=str), line_id,
        )

        status = await conn.execute(
            f"DELETE FROM {table} WHERE box_id = $1 AND transaction_no = $2",
            line.box_id, line.transaction_no,
        )
        removed = int(status.split()[-1]) if status and status.split()[-1].isdigit() else 0
        if removed > 1:
            # Duplicate inventory rows for one physical box: the snapshot holds
            # only the first, so a later restore would under-restore.
            logger.warning("Challan %s: %d rows in %s matched box %s (%s) — expected 1",
                           challan_no, removed, table, line.box_id, line.transaction_no)
        deducted += removed

        await _write_disposition(
            conn, box_id=line.box_id, transaction_no=line.transaction_no,
            cold_row=dict(cold_row), line=line, source_table=table,
            header_id=header_id, challan_no=challan_no, disposed_by=disposed_by,
        )

    return deducted


# ── Entry point ──────────────────────────────────────────────────────────────
async def create_out(conn, data: schemas.JobWorkCreateRequest, raw_payload: dict,
                     created_by: str) -> dict:
    """Create a Material Out challan and deduct its boxes from cold storage.

    Atomic: header, lines and every cold_stocks DELETE commit or roll back
    together.
    """
    _assert_boxes_unique(data.line_items)

    async with conn.transaction():
        await _assert_challan_unused(conn, data.header.challan_no)
        header_id = await _insert_header(conn, data, raw_payload, created_by)
        line_ids = [await _insert_line(conn, header_id, line) for line in data.line_items]
        deducted = await _deduct_cold_storage(
            conn, header_id=header_id, challan_no=data.header.challan_no,
            disposed_by=created_by, lines=data.line_items, line_ids=line_ids,
        )

    logger.info("Job Work OUT %s created (id=%s, %d lines, %d cold boxes deducted) by %s",
                data.header.challan_no, header_id, len(line_ids), deducted, created_by)

    # Read back AFTER commit so a POST response is identical to a later GET.
    record = await q.get_job_work(conn, header_id)
    record["cold_boxes_deducted"] = deducted
    return record
