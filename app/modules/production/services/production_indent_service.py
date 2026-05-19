"""Production Indent (FG/SFG) service — Section A2."""
from datetime import datetime, timezone


def _gen_id(seq_val: int) -> str:
    d = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"PRDI-{d}-{seq_val:04d}"


def _gen_int_ord_id(seq_val: int) -> str:
    d = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"INT-ORD-{d}-{seq_val:04d}"


def _gen_int_jc_id(seq_val: int) -> str:
    d = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"IJC-{d}-{seq_val:04d}"


# ── List ──
async def list_production_indents(conn, *, entity=None, status=None,
                                   search=None, date_from=None, date_to=None,
                                   page=1, page_size=50):
    conditions = []
    params = []
    idx = 1

    if entity:
        conditions.append(f"entity = ${idx}")
        params.append(entity)
        idx += 1
    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1
    if search:
        conditions.append(f"(item_description ILIKE ${idx} OR customer_name ILIKE ${idx} OR triggered_by_so ILIKE ${idx} OR prod_indent_id ILIKE ${idx})")
        params.append(f"%{search}%")
        idx += 1
    if date_from:
        conditions.append(f"created_at >= ${idx}")
        params.append(date_from)
        idx += 1
    if date_to:
        conditions.append(f"created_at <= ${idx}::date + INTERVAL '1 day'")
        params.append(date_to)
        idx += 1

    where = " WHERE " + " AND ".join(conditions) if conditions else ""

    total = await conn.fetchval(f"SELECT COUNT(*) FROM production_indent{where}", *params)

    rows = await conn.fetch(f"""
        SELECT * FROM production_indent{where}
        ORDER BY created_at DESC
        LIMIT ${idx} OFFSET ${idx + 1}
    """, *params, page_size, (page - 1) * page_size)

    return {
        "results": [dict(r) for r in rows],
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": max(1, -(-total // page_size)),
        },
    }


# ── Get single ──
async def get_production_indent(conn, indent_id):
    row = await conn.fetchrow(
        "SELECT * FROM production_indent WHERE prod_indent_id = $1 OR id::text = $1",
        str(indent_id),
    )
    if not row:
        return None
    return dict(row)


# ── Create ──
async def create_production_indent(conn, *, item_description, material_type,
                                    uom="kg", required_qty, available_qty=0,
                                    shortfall_qty=0, triggered_by_job_card=None,
                                    triggered_by_so=None, customer_name=None,
                                    maker_user, status="draft", entity="cfpl"):
    # Duplicate prevention
    if triggered_by_so:
        existing = await conn.fetchval("""
            SELECT prod_indent_id FROM production_indent
            WHERE item_description = $1 AND triggered_by_so = $2
              AND status NOT IN ('fulfilled', 'cancelled')
        """, item_description, triggered_by_so)
        if existing:
            return {"error": f"Open indent already exists: {existing}", "duplicate": True}

    seq = await conn.fetchval("SELECT nextval('seq_prdi')")
    pid = _gen_id(seq)

    await conn.execute("""
        INSERT INTO production_indent
            (prod_indent_id, item_description, material_type, uom,
             required_qty, available_qty, shortfall_qty,
             triggered_by_job_card, triggered_by_so, customer_name,
             maker_user, status, entity)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
    """, pid, item_description, material_type, uom,
         required_qty, available_qty, shortfall_qty,
         triggered_by_job_card, triggered_by_so, customer_name,
         maker_user, status, entity)

    return {"prod_indent_id": pid, "status": status}


# ── Submit (draft → submitted) ──
async def submit_indent(conn, indent_id):
    r = await conn.execute("""
        UPDATE production_indent SET status = 'submitted'
        WHERE (prod_indent_id = $1 OR id::text = $1) AND status = 'draft'
    """, str(indent_id))
    return {"updated": r == "UPDATE 1"}


# ── Approve (submitted → approved) ──
async def approve_indent(conn, indent_id, *, checker_user, checker_comment=""):
    r = await conn.execute("""
        UPDATE production_indent
        SET status = 'approved', checker_user = $2, checker_comment = $3,
            approved_at = NOW()
        WHERE (prod_indent_id = $1 OR id::text = $1) AND status = 'submitted'
    """, str(indent_id), checker_user, checker_comment)
    return {"updated": r == "UPDATE 1"}


# ── Return (submitted → draft) ──
async def return_indent(conn, indent_id, *, checker_user, checker_comment):
    r = await conn.execute("""
        UPDATE production_indent
        SET status = 'draft', checker_user = $2, checker_comment = $3
        WHERE (prod_indent_id = $1 OR id::text = $1) AND status = 'submitted'
    """, str(indent_id), checker_user, checker_comment)
    return {"updated": r == "UPDATE 1"}


# ── Cancel ──
async def cancel_indent(conn, indent_id, *, cancel_reason):
    r = await conn.execute("""
        UPDATE production_indent
        SET status = 'cancelled', cancel_reason = $2
        WHERE (prod_indent_id = $1 OR id::text = $1) AND status NOT IN ('fulfilled','cancelled')
    """, str(indent_id), cancel_reason)
    return {"updated": r == "UPDATE 1"}


# ── Create Internal Order + JC (approved → internal_jc_created) ──
async def create_internal_order(conn, indent_id):
    row = await conn.fetchrow("""
        SELECT * FROM production_indent
        WHERE (prod_indent_id = $1 OR id::text = $1) AND status = 'approved'
    """, str(indent_id))
    if not row:
        return {"error": "Indent not found or not in approved status"}

    # Create internal order
    ord_seq = await conn.fetchval("SELECT nextval('seq_int_ord')")
    ord_id = _gen_int_ord_id(ord_seq)

    await conn.execute("""
        INSERT INTO internal_order
            (internal_order_id, prod_indent_id, item_description,
             material_type, required_qty, entity)
        VALUES ($1,$2,$3,$4,$5,$6)
    """, ord_id, row["prod_indent_id"], row["item_description"],
         row["material_type"], row["required_qty"],
         row["entity"] or "cfpl")

    # Create internal job card
    jc_seq = await conn.fetchval("SELECT nextval('seq_int_jc')")
    jc_id = _gen_int_jc_id(jc_seq)

    # Attempt BOM explosion
    bom = await conn.fetchrow("""
        SELECT bom_id, fg_sku_name FROM bom_header
        WHERE fg_sku_name ILIKE $1 AND is_active = TRUE
        ORDER BY version DESC LIMIT 1
    """, f"%{row['item_description']}%")

    bom_data = None
    if bom:
        lines = await conn.fetch("""
            SELECT * FROM bom_line WHERE bom_id = $1 ORDER BY line_number
        """, bom["bom_id"])
        bom_data = {"bom_id": bom["bom_id"], "lines": [dict(l) for l in lines]}

    await conn.execute("""
        INSERT INTO internal_job_card
            (internal_jc_id, internal_order_id, parent_job_card_id,
             parent_so_ref, fg_sku_name, bom_data, entity)
        VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7)
    """, jc_id, ord_id, row["triggered_by_job_card"],
         row["triggered_by_so"], row["item_description"],
         __import__("json").dumps(bom_data) if bom_data else None,
         row["entity"] or "cfpl")

    # Update indent
    await conn.execute("""
        UPDATE production_indent
        SET status = 'internal_jc_created',
            linked_internal_order = $2, linked_internal_jc = $3
        WHERE prod_indent_id = $1
    """, row["prod_indent_id"], ord_id, jc_id)

    # Update internal order status
    await conn.execute("""
        UPDATE internal_order SET status = 'jc_assigned' WHERE internal_order_id = $1
    """, ord_id)

    return {
        "internal_order_id": ord_id,
        "internal_jc_id": jc_id,
        "bom_found": bom_data is not None,
    }
