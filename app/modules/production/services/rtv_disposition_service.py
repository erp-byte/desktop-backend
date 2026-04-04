"""RTV Disposition service — Section H1-H4."""
import json
from datetime import datetime, timezone


def _gen_disp_id(seq_val: int) -> str:
    d = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"RTVD-{d}-{seq_val:04d}"


def _gen_og_id(seq_val: int) -> str:
    d = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"OG-{d}-{seq_val:04d}"


# ── List RTV dispositions ──
async def list_dispositions(conn, *, entity=None, status=None):
    conditions = []
    params = []
    idx = 1

    if entity:
        conditions.append(f"entity = ${idx}")
        params.append(entity)
        idx += 1

    # If status = 'approved', show RTVs pending disposition
    # Otherwise filter by disposition_type
    if status and status != "approved":
        conditions.append(f"disposition_type = ${idx}")
        params.append(status)
        idx += 1

    where = " WHERE " + " AND ".join(conditions) if conditions else ""

    rows = await conn.fetch(f"""
        SELECT * FROM rtv_disposition{where}
        ORDER BY
            CASE WHEN disposition_type = 'pending' THEN 0 ELSE 1 END,
            created_at DESC
        LIMIT 200
    """, *params)

    return {"results": [dict(r) for r in rows]}


# ── Assign disposition ──
async def assign_disposition(conn, *, rtv_id, disposition_type, decided_by,
                              qc_remarks=None, entity="cfpl"):
    seq = await conn.fetchval("SELECT nextval('seq_rtvd')")
    disp_id = _gen_disp_id(seq)
    now = datetime.now(timezone.utc)

    linked_internal_order = None
    linked_offgrade_lot = None

    # Route based on disposition type
    if disposition_type == "reprocess":
        # Auto-create a production indent for reprocessing
        from .production_indent_service import create_production_indent
        result = await create_production_indent(
            conn,
            item_description=f"Reprocess: RTV-{rtv_id}",
            material_type="FG",
            required_qty=0,  # Will be filled from RTV detail
            maker_user=decided_by,
            status="submitted",
            entity=entity,
        )
        if not result.get("error"):
            linked_internal_order = result.get("prod_indent_id")

    elif disposition_type == "offgrade":
        # Create off-grade inventory entry
        og_seq = await conn.fetchval("SELECT nextval('seq_ogi')")
        og_id = _gen_og_id(og_seq)

        await conn.execute("""
            INSERT INTO off_grade_inventory
                (offgrade_id, item_description, source_type, source_id,
                 condition_notes, entity)
            VALUES ($1, $2, 'RTV', $3, $4, $5)
        """, og_id, f"RTV-{rtv_id}", rtv_id, qc_remarks, entity)
        linked_offgrade_lot = og_id

    elif disposition_type == "discard":
        pass  # Discard requires separate management approval

    # Insert disposition record
    await conn.execute("""
        INSERT INTO rtv_disposition
            (disposition_id, rtv_id, disposition_type, decided_by,
             decided_at, qc_remarks, linked_internal_order,
             linked_offgrade_lot, entity)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
    """, disp_id, rtv_id, disposition_type, decided_by, now,
         qc_remarks, linked_internal_order, linked_offgrade_lot, entity)

    return {
        "disposition_id": disp_id,
        "disposition_type": disposition_type,
        "linked_internal_order": linked_internal_order,
        "linked_offgrade_lot": linked_offgrade_lot,
    }


# ── Approve discard (Management only) ──
async def approve_discard(conn, *, rtv_id, reason, authorised_by):
    # Find the disposition
    disp = await conn.fetchrow("""
        SELECT * FROM rtv_disposition
        WHERE rtv_id = $1 AND disposition_type = 'discard'
        ORDER BY created_at DESC LIMIT 1
    """, rtv_id)

    if not disp:
        return {"error": "No discard disposition found for this RTV"}

    # Mark as approved
    await conn.execute("""
        UPDATE rtv_disposition SET discard_approved = TRUE
        WHERE disposition_id = $1
    """, disp["disposition_id"])

    # Create write-off ledger entry
    await conn.execute("""
        INSERT INTO write_off_ledger
            (rtv_id, item_description, reason, authorised_by)
        VALUES ($1, $2, $3, $4)
    """, rtv_id, disp.get("item_description") or f"RTV-{rtv_id}",
         reason, authorised_by)

    return {"discarded": True, "disposition_id": disp["disposition_id"]}
