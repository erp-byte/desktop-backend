"""Sample requisition lifecycle (checklist A4 / spec §8).

Owns the requisition record + its article lines and the guarded status state
machine. Approvals, outward movements, job cards, gate passes and conversions
live in sibling services and call transition_status() here to move the record.

Each requisition is identified by request_id — an app-supplied 8-digit time-based
BIGINT (new_short_time_id, the house id pattern shared with job_card_id / plan_id)
and the PRIMARY KEY since migration 057. It is the sole surfaced identifier.
"""
from __future__ import annotations

import logging

import asyncpg
from fastapi import HTTPException

from app.core.helpers import new_short_time_id
from app.modules.sample.services import audit_service, notification_service

logger = logging.getLogger(__name__)

WAREHOUSES = ("W202", "A185", "A68", "F53", "A101", "D-39", "D-514", "Rishi", "Supreme")

SAMPLE_TYPES = ("BASIS_RM", "BASIS_FG", "NPD", "INTERNAL", "TRIAL")

# Status state machine (spec §8). Maps current -> set of allowed next states.
TRANSITIONS: dict[str, set[str]] = {
    "DRAFT":                 {"SUBMITTED", "CANCELLED"},
    # NPD review of a BH-sent request: approve / reject / hold.
    "SUBMITTED":             {"BH_APPROVED", "BH_REJECTED", "ON_HOLD", "CANCELLED"},
    "ON_HOLD":               {"BH_APPROVED", "BH_REJECTED", "SUBMITTED", "CANCELLED"},
    "BH_REJECTED":           {"SUBMITTED", "CANCELLED"},
    "BH_APPROVED":           {"IN_PRODUCTION", "READY_FOR_DISPATCH", "CANCELLED"},
    "IN_PRODUCTION":         {"PACKING", "CANCELLED"},
    "PACKING":               {"READY_FOR_DISPATCH", "CANCELLED"},
    "READY_FOR_DISPATCH":    {"GATE_PASS_ISSUED", "INTERNALLY_DISPATCHED", "CANCELLED"},
    "GATE_PASS_ISSUED":      {"CLOSED"},
    "INTERNALLY_DISPATCHED": {"GATE_PASS_ISSUED", "PARTIALLY_CONVERTED", "CLOSED"},
    "PARTIALLY_CONVERTED":   {"GATE_PASS_ISSUED", "CLOSED"},
    "CLOSED":                set(),
    "CANCELLED":             set(),
}


def _assert_transition(current: str, target: str) -> None:
    """Raise 409 if current -> target is not an allowed status transition."""
    if target not in TRANSITIONS.get(current, set()):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "illegal_transition",
                "message": f"Cannot move requisition from {current} to {target}",
                "details": {"current": current, "target": target},
            },
        )


async def _fetch_req(conn, req_id: int) -> dict | None:
    row = await conn.fetchrow(
        "SELECT * FROM sample_requisitions WHERE id = $1 AND deleted_at IS NULL",
        req_id,
    )
    return dict(row) if row else None


def _require(req: dict | None, req_id: int) -> dict:
    if req is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found",
                    "message": f"Sample requisition {req_id} not found",
                    "details": {"id": req_id}},
        )
    return req


# ---------------------------------------------------------------------------
# Create / read / list
# ---------------------------------------------------------------------------
async def create_requisition(conn, *, payload: dict, user) -> dict:
    """Insert a DRAFT requisition + its article lines. One transaction."""
    sample_type = payload["sample_type"]
    if sample_type not in SAMPLE_TYPES:
        raise HTTPException(422, detail={"error": "invalid_sample_type",
                                         "message": f"sample_type must be one of {SAMPLE_TYPES}",
                                         "details": {"sample_type": sample_type}})
    warehouse = payload.get("warehouse")
    if warehouse not in WAREHOUSES:
        raise HTTPException(422, detail={"error": "invalid_warehouse",
                                         "message": f"warehouse must be one of {WAREHOUSES}",
                                         "details": {"warehouse": warehouse}})

    articles = payload.get("articles") or []

    # Multiple NPD target articles (each its own product + qty). The requisition
    # HEADER (npd_target_name / pcs / weight_per_piece / quantity) mirrors targets[0]
    # for backward compat; the full list goes to the child table after insert.
    targets = payload.get("targets")
    if not targets and payload.get("npd_target_name"):     # legacy single-target payload
        targets = [{"name": payload["npd_target_name"], "pcs": payload.get("pcs"),
                    "weight_per_piece": payload.get("weight_per_piece")}]
    targets = _derive_targets(targets)
    if sample_type in ("NPD", "TRIAL") and not targets:
        raise HTTPException(422, detail={"error": "no_target",
                                         "message": "At least one target article is required for an NPD/TRIAL requisition",
                                         "details": {}})

    # Quantity is derived from pcs × weight_per_piece when both are given (the NPD
    # request captures pieces + per-piece weight); otherwise the sent quantity. When
    # targets are present the header mirrors target #1.
    if targets:
        payload = {**payload, "npd_target_name": targets[0]["name"]}
        pcs = targets[0].get("pcs")
        wpp = targets[0].get("weight_per_piece")
        quantity = targets[0].get("quantity")
    else:
        pcs = payload.get("pcs")
        wpp = payload.get("weight_per_piece")
        quantity = payload.get("quantity")
        if pcs is not None and wpp is not None:
            quantity = round(float(pcs) * float(wpp), 3)

    # Requestor / POC split (085): the form may name the business head the request is
    # raised FOR. When it does, that BH becomes requestor_user_id AND business_head_user_id
    # (so the BASIS approval gate binds to them), requestor_team mirrors their name for
    # display, and the creator is recorded as the POC. With no BH named, the creator stays
    # the requestor — the pre-085 behaviour — and business_head_user_id is left NULL rather
    # than guessed, since a wrong binding would gate approval on the wrong person.
    bh_uid = payload.get("requestor_user_id")
    requestor_uid = user.user_id
    requestor_team = payload.get("requestor_team")
    if bh_uid is not None:
        bh_name = await _assert_business_head(conn, bh_uid)
        requestor_uid = bh_uid
        requestor_team = bh_name
    poc_uid, poc_name, poc_email = await _resolve_sales_poc(conn, payload, user)

    async with conn.transaction():
        # request_id is an app-supplied 8-digit time-based BIGINT (new_short_time_id,
        # the same pattern as job_card_id / plan_id) and the surfaced identifier. It
        # is the PRIMARY KEY (migration 057); retry on the rare unique collision via
        # a per-attempt savepoint.
        req_id = None
        for _attempt in range(5):
            request_id = new_short_time_id()
            try:
                async with conn.transaction():   # savepoint for the unique retry
                    req_id = await conn.fetchval(
                        """
                        INSERT INTO sample_requisitions
                            (request_id, sample_type, status, requestor_user_id,
                             business_head_user_id,
                             requestor_team, purpose_tag, purpose_note, base_bom_id,
                             internal_override, warehouse, transporter_name, vehicle_number,
                             npd_target_name, quantity, description,
                             company_name, customer_name, customer_contact, customer_ship_to_address,
                             mode_of_transport, expected_dispatch_date, confirmed_dispatch_date,
                             pcs, weight_per_piece,
                             returnable, non_returnable, paid, amount,
                             created_by, updated_by)
                        VALUES ($1, $2, 'DRAFT', $3, $28, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                                $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $29, $29)
                        RETURNING id
                        """,
                        request_id, sample_type, requestor_uid,
                        requestor_team, payload.get("purpose_tag"),
                        payload.get("purpose_note"), payload.get("base_bom_id"),
                        bool(payload.get("internal_override", False)), warehouse,
                        payload.get("transporter_name"), payload.get("vehicle_number"),
                        payload.get("npd_target_name"), quantity,
                        payload.get("description"),
                        payload.get("company_name"), payload.get("customer_name"),
                        payload.get("customer_contact"), payload.get("customer_ship_to_address"),
                        payload.get("mode_of_transport"), payload.get("expected_dispatch_date"),
                        payload.get("confirmed_dispatch_date"),
                        pcs, wpp,
                        # Billing checklist — coerce to non-null (columns are NOT NULL);
                        # NpdRequisitionCreate has already normalised amount to 0 when
                        # not paid. A non-billing create (generic RM/FG/INTERNAL) sends
                        # nothing → (FALSE, FALSE, FALSE, 0).
                        bool(payload.get("returnable")), bool(payload.get("non_returnable")),
                        bool(payload.get("paid")), payload.get("amount") or 0,
                        bh_uid, user.user_id,
                    )
                break
            except asyncpg.UniqueViolationError:
                if _attempt == 4:
                    raise
        # Sales POC is written separately so the INSERT above stays valid on an environment
        # where 085 has not been hand-applied yet (see has_sales_poc_columns).
        if (poc_uid or poc_name or poc_email) and await has_sales_poc_columns(conn):
            await conn.execute(
                """UPDATE sample_requisitions
                      SET sales_poc_user_id = $2, sales_poc_name = $3, sales_poc_email = $4
                    WHERE id = $1""", req_id, poc_uid, poc_name, poc_email)
        await _insert_articles(conn, req_id, articles)
        await _insert_npd_targets(conn, req_id, targets)
        await audit_service.write_audit(
            conn, req_id, audit_service.EV_STATUS_CHANGE,
            new_value={"status": "DRAFT", "request_id": request_id},
            actor_user_id=user.user_id, actor_role=user.role_name,
            remarks="Requisition created",
        )
    created = await get_requisition(conn, req_id)
    try:
        from app.modules.sample.services import sample_mail_service as mail
        # Roots the transaction's mail trail — every later NPD mail (review, outcome,
        # promote gates, promote decision, dispatch) replies into this one.
        await mail.notify_requisition_event(conn, created, event="created")
    except Exception:  # noqa: BLE001
        logger.exception("Requisition created email failed for req %s", req_id)
    return created


async def _insert_articles(conn, req_id: int, articles: list[dict]) -> None:
    for a in articles:
        if not a.get("sku_id"):
            # Free-text articles are rejected at the boundary (spec §15.1) —
            # a real sku_id from /api/v1/so/sku-lookup is mandatory.
            raise HTTPException(422, detail={
                "error": "invalid_article",
                "message": "Each article must carry a sku_id from the SKU lookup",
                "details": {"article": a}})
        if float(a.get("required_qty") or 0) <= 0:
            raise HTTPException(422, detail={
                "error": "invalid_qty",
                "message": "required_qty must be > 0",
                "details": {"article": a}})
        await conn.execute(
            """
            INSERT INTO sample_requisition_articles
                (requisition_id, sku_id, sku_name, required_qty, uom,
                 article_role, pack_size_kg, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            req_id, a["sku_id"], a["sku_name"], a["required_qty"],
            a["uom"], a["article_role"], a.get("pack_size_kg"), a.get("notes"),
        )


async def _insert_npd_targets(conn, req_id: int, targets: list[dict], *, replace: bool = False) -> None:
    """Persist the requisition's NPD target articles (080). replace=True wipes the
    existing rows first (edit). Best-effort: a missing child table (080 not applied)
    or a bad row must not break the create/update — the header mirror still carries
    target #1, so the requisition stays usable. The savepoint keeps a failure here from
    poisoning the outer transaction (which would then fail write_audit / the header)."""
    if not targets and not replace:
        return
    try:
        async with conn.transaction():   # savepoint
            if replace:
                await conn.execute(
                    "DELETE FROM sample_requisition_npd_targets WHERE requisition_id = $1", req_id)
            for i, t in enumerate(targets or []):
                await conn.execute(
                    """INSERT INTO sample_requisition_npd_targets
                           (requisition_id, name, pcs, weight_per_piece, quantity, line_order)
                       VALUES ($1, $2, $3, $4, $5, $6)""",
                    req_id, t["name"], t.get("pcs"), t.get("weight_per_piece"),
                    t.get("quantity"), i)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to write NPD targets for requisition %s (migration 080 applied?)", req_id)


def _derive_targets(targets: list[dict] | None) -> list[dict]:
    """Normalise a targets payload: copy each row and derive quantity = pcs × weight."""
    out = [dict(t) for t in (targets or [])]
    for t in out:
        tp, tw = t.get("pcs"), t.get("weight_per_piece")
        t["quantity"] = round(float(tp) * float(tw), 3) if tp is not None and tw is not None else None
    return out


async def _sync_first_npd_target(conn, req_id: int) -> None:
    """After a HEADER-only target edit (npd_target_name/pcs/weight — e.g. the NPD list
    page's quick-edit, which doesn't send `targets`), mirror the header onto the FIRST
    child target row so the detail target list reflects it — leaving targets #2..n
    untouched. Inserts a row if the requisition had none. Best-effort savepoint."""
    try:
        async with conn.transaction():   # savepoint
            hdr = await conn.fetchrow(
                "SELECT npd_target_name, pcs, weight_per_piece, quantity "
                "FROM sample_requisitions WHERE id = $1", req_id)
            if not hdr or not hdr["npd_target_name"]:
                return
            first = await conn.fetchrow(
                "SELECT id FROM sample_requisition_npd_targets WHERE requisition_id = $1 "
                "ORDER BY line_order, id LIMIT 1", req_id)
            if first:
                await conn.execute(
                    "UPDATE sample_requisition_npd_targets "
                    "SET name = $2, pcs = $3, weight_per_piece = $4, quantity = $5 WHERE id = $1",
                    first["id"], hdr["npd_target_name"], hdr["pcs"], hdr["weight_per_piece"], hdr["quantity"])
            else:
                await conn.execute(
                    """INSERT INTO sample_requisition_npd_targets
                           (requisition_id, name, pcs, weight_per_piece, quantity, line_order)
                       VALUES ($1, $2, $3, $4, $5, 0)""",
                    req_id, hdr["npd_target_name"], hdr["pcs"], hdr["weight_per_piece"], hdr["quantity"])
    except Exception:  # noqa: BLE001
        logger.exception("Failed to sync first NPD target for requisition %s", req_id)


async def _npd_targets_for(conn, req: dict) -> list[dict]:
    """The requisition's target articles (080). Legacy requisitions (pre-080) have no
    child rows — synthesize a single target from the header mirror so the UI still
    shows it. Best-effort read: a missing table degrades to the header synthesis."""
    rows: list = []
    try:
        rows = await conn.fetch(
            "SELECT id, name, pcs, weight_per_piece, quantity, line_order "
            "FROM sample_requisition_npd_targets WHERE requisition_id = $1 ORDER BY line_order, id",
            req["id"])
    except Exception:  # noqa: BLE001
        logger.exception("Failed to read NPD targets for requisition %s (migration 080 applied?)", req["id"])
    if rows:
        return [dict(r) for r in rows]
    if req.get("npd_target_name"):
        return [{"name": req["npd_target_name"], "pcs": req.get("pcs"),
                 "weight_per_piece": req.get("weight_per_piece"), "quantity": req.get("quantity")}]
    return []


async def get_requisition(conn, req_id: int) -> dict:
    req = _require(await _fetch_req(conn, req_id), req_id)
    req["npd_targets"] = await _npd_targets_for(conn, req)
    articles = await conn.fetch(
        "SELECT * FROM sample_requisition_articles WHERE requisition_id = $1 ORDER BY id",
        req_id)
    approvals = await conn.fetch(
        "SELECT * FROM sample_approvals WHERE requisition_id = $1 ORDER BY sequence_no",
        req_id)
    audit = await conn.fetch(
        "SELECT * FROM sample_audit_log WHERE requisition_id = $1 ORDER BY created_at",
        req_id)
    req["articles"] = [dict(r) for r in articles]
    req["approvals"] = [dict(r) for r in approvals]
    req["audit"] = [dict(r) for r in audit]
    return req


async def list_requisitions(conn, *, status: str | None = None,
                            sample_type: str | None = None,
                            warehouse: str | None = None,
                            sample_types: list[str] | None = None,
                            statuses: list[str] | None = None,
                            requestor: str | None = None,
                            q: str | None = None,
                            date_from=None, date_to=None,
                            limit: int = 50, offset: int = 0) -> list[dict]:
    """List requisitions with the queue filters.

    `sample_type` keeps the legacy single-type filter; `sample_types` narrows to a
    set (the NPD queue passes NPD/TRIAL). `statuses` filters to a set of statuses
    (the NPD queue maps its 3 review buckets — Pending/Hold/Accepted — onto the
    underlying lifecycle states). `q` is a free-text search across request_id,
    target article, description and requestor. `date_from` / `date_to`
    bound created_at (inclusive, by calendar date). Each row carries `hold_reason`
    — the most recent HOLD remark — so the queue can surface it on the Hold pill.
    """
    rows = await conn.fetch(
        """
        SELECT sr.*,
               (SELECT a.remarks FROM sample_approvals a
                 WHERE a.requisition_id = sr.id AND a.action = 'HOLD'
                 ORDER BY a.actioned_at DESC NULLS LAST, a.sequence_no DESC
                 LIMIT 1) AS hold_reason
          FROM sample_requisitions sr
         WHERE sr.deleted_at IS NULL
          AND ($1::text   IS NULL OR sr.status = $1)
          AND ($2::text   IS NULL OR sr.sample_type = $2)
          AND ($3::text   IS NULL OR sr.warehouse = $3)
          AND ($4::text[] IS NULL OR sr.sample_type = ANY($4))
          AND ($5::text   IS NULL OR sr.requestor_team = $5)
          AND ($6::date   IS NULL OR sr.created_at::date >= $6)
          AND ($7::date   IS NULL OR sr.created_at::date <= $7)
          AND ($8::text   IS NULL OR (
                sr.request_id::text                ILIKE '%' || $8 || '%'
             OR COALESCE(sr.npd_target_name, '')   ILIKE '%' || $8 || '%'
             OR COALESCE(sr.description, '')        ILIKE '%' || $8 || '%'
             OR COALESCE(sr.requestor_team, '')     ILIKE '%' || $8 || '%'
          ))
          AND ($9::text[] IS NULL OR sr.status = ANY($9))
         ORDER BY sr.created_at DESC
         LIMIT $10 OFFSET $11
        """,
        status, sample_type, warehouse, sample_types, requestor,
        date_from, date_to, q, statuses, limit, offset)
    return [dict(r) for r in rows]


async def list_requestors(conn, *, sample_types: list[str] | None = None) -> list[str]:
    """Distinct requestor labels — feeds the queue's Requestor filter dropdown."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT requestor_team
          FROM sample_requisitions
         WHERE deleted_at IS NULL
           AND requestor_team IS NOT NULL AND requestor_team <> ''
           AND ($1::text[] IS NULL OR sample_type = ANY($1))
         ORDER BY requestor_team
        """,
        sample_types)
    return [r["requestor_team"] for r in rows]


async def list_business_heads(conn) -> list[dict]:
    """Active users holding the business_head role (primary or additional) — feeds the
    requestor dropdown on the NPD request form, where a sales/admin user raises a
    requisition ON BEHALF OF a business head.

    Returns {user_id, full_name}: the id is what create/update bind requestor_user_id and
    business_head_user_id to, so the BH approval and the mail trail's To line route to that
    specific person rather than being matched on a display string."""
    rows = await conn.fetch(
        """SELECT DISTINCT u.user_id, u.full_name
             FROM auth_user u
             LEFT JOIN auth_role pr ON u.role_id = pr.role_id
             LEFT JOIN auth_user_role ur ON ur.user_id = u.user_id
             LEFT JOIN auth_role r ON ur.role_id = r.role_id
            WHERE COALESCE(u.is_active, TRUE)
              AND u.full_name IS NOT NULL AND btrim(u.full_name) <> ''
              AND (pr.role_name = 'business_head' OR r.role_name = 'business_head')
            ORDER BY u.full_name""")
    return [{"user_id": r["user_id"], "full_name": r["full_name"]} for r in rows]


async def _assert_business_head(conn, uid: int) -> str:
    """Full name of `uid`, or 422 if they are not an active business head. Guards the
    requestor field at the boundary: the dropdown offers BHs only, so anything else
    arriving here is a hand-crafted payload, and letting it through would bind the BASIS
    approval gate to someone who can never clear it."""
    row = await conn.fetchrow(
        """SELECT u.full_name
             FROM auth_user u
             LEFT JOIN auth_role pr ON u.role_id = pr.role_id
             LEFT JOIN auth_user_role ur ON ur.user_id = u.user_id
             LEFT JOIN auth_role r ON ur.role_id = r.role_id
            WHERE u.user_id = $1 AND COALESCE(u.is_active, TRUE)
              AND (pr.role_name = 'business_head' OR r.role_name = 'business_head')
            LIMIT 1""", uid)
    if row is None:
        raise HTTPException(422, detail={
            "error": "invalid_requestor",
            "message": "requestor_user_id must be an active business head",
            "details": {"requestor_user_id": uid}})
    return row["full_name"]


async def has_sales_poc_columns(conn) -> bool:
    """Whether migration 085 is applied. samples/ migrations are hand-applied (see the
    header of 072), so every column they add has to be optional in code — mirrors the
    information_schema guard npd_dev_service uses for the 084 `uom` column. Without this
    an unmigrated environment would 500 on every requisition create."""
    return bool(await conn.fetchval(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'sample_requisitions' AND column_name = 'sales_poc_user_id'"))


async def list_sales_pocs(conn) -> list[dict]:
    """Active users holding the `sales` role — feeds the Sales POC dropdown. Returns
    {user_id, full_name, email}: the email is what the mail trail Ccs, so a POC with no
    address on file is still selectable but simply never receives the trail."""
    rows = await conn.fetch(
        """SELECT DISTINCT u.user_id, u.full_name, COALESCE(u.email, '') AS email
             FROM auth_user u
             LEFT JOIN auth_role pr ON u.role_id = pr.role_id
             LEFT JOIN auth_user_role ur ON ur.user_id = u.user_id
             LEFT JOIN auth_role r ON ur.role_id = r.role_id
            WHERE COALESCE(u.is_active, TRUE)
              AND u.full_name IS NOT NULL AND btrim(u.full_name) <> ''
              AND (pr.role_name = 'sales' OR r.role_name = 'sales')
            ORDER BY u.full_name""")
    return [{"user_id": r["user_id"], "full_name": r["full_name"], "email": r["email"]}
            for r in rows]


async def _resolve_sales_poc(conn, payload: dict, user) -> tuple[int | None, str | None, str | None]:
    """(user_id, name, email) for the request's sales POC.

    A named user wins and has their name/email read from auth_user, so the stored snapshot
    can never disagree with the account. Free-text name/email is still accepted for a POC
    without a login (same escape hatch Customer Returns has). With nothing supplied the
    signed-in user becomes the POC — the common case, since `sales` is the role that
    raises these requests. Any user may be named: unlike the requestor BH this drives no
    approval gate, only display and a Cc, so restricting it would add friction for no
    safety gain."""
    uid = payload.get("sales_poc_user_id")
    if uid is not None:
        row = await conn.fetchrow(
            "SELECT full_name, email FROM auth_user WHERE user_id = $1", uid)
        if row is None:
            raise HTTPException(422, detail={
                "error": "invalid_sales_poc",
                "message": "sales_poc_user_id does not match a user",
                "details": {"sales_poc_user_id": uid}})
        return uid, row["full_name"], (row["email"] or None)
    name = (payload.get("sales_poc_name") or "").strip()
    email = (payload.get("sales_poc_email") or "").strip()
    if name or email:
        return None, name or None, email or None
    return (user.user_id,
            (getattr(user, "full_name", None) or "").strip() or None,
            (getattr(user, "email", None) or "").strip() or None)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------
async def update_requisition(conn, req_id: int, *, payload: dict, user) -> dict:
    """Edit a DRAFT or BH_REJECTED requisition (header + article replacement)."""
    req = _require(await _fetch_req(conn, req_id), req_id)
    if req["status"] not in ("DRAFT", "SUBMITTED", "BH_REJECTED"):
        raise HTTPException(409, detail={
            "error": "not_editable",
            "message": "Only DRAFT, SUBMITTED or BH_REJECTED requisitions can be edited",
            "details": {"status": req["status"]}})
    new_warehouse = payload.get("warehouse")
    if new_warehouse is not None and new_warehouse not in WAREHOUSES:
        raise HTTPException(422, detail={"error": "invalid_warehouse",
                                         "message": f"warehouse must be one of {WAREHOUSES}",
                                         "details": {"warehouse": new_warehouse}})
    # Multiple NPD target articles — when the patch sends `targets`, re-derive the
    # header mirror (npd_target_name / pcs / weight / quantity) from target #1 and
    # replace the child rows below. targets is None when the patch doesn't touch them.
    targets = _derive_targets(payload.get("targets")) if payload.get("targets") is not None else None
    if req["sample_type"] in ("NPD", "TRIAL") and targets is not None and not targets:
        raise HTTPException(422, detail={"error": "no_target",
                                         "message": "At least one target article is required"})
    if targets:
        payload = {**payload, "npd_target_name": targets[0]["name"],
                   "pcs": targets[0].get("pcs"), "weight_per_piece": targets[0].get("weight_per_piece")}
    # Recompute quantity from the merged pcs × weight_per_piece (existing values
    # used where the patch omits one). Falls back to the sent quantity otherwise.
    eff_pcs = payload.get("pcs") if payload.get("pcs") is not None else req.get("pcs")
    eff_wpp = (payload.get("weight_per_piece") if payload.get("weight_per_piece") is not None
               else req.get("weight_per_piece"))
    quantity = payload.get("quantity")
    if eff_pcs is not None and eff_wpp is not None:
        quantity = round(float(eff_pcs) * float(eff_wpp), 3)
    # Billing invariants on the MERGED state (patch over existing) so a partial PATCH
    # can't drive the row into a state the DB CHECK rejects — surface a clean 422
    # instead of an IntegrityError. (A False patch value must win, so test `is not None`.)
    eff_returnable = payload["returnable"] if payload.get("returnable") is not None else req.get("returnable")
    eff_non_returnable = payload["non_returnable"] if payload.get("non_returnable") is not None else req.get("non_returnable")
    eff_paid = payload["paid"] if payload.get("paid") is not None else req.get("paid")
    eff_amount = 0 if payload.get("paid") is False else (
        payload["amount"] if payload.get("amount") is not None else req.get("amount"))
    if eff_returnable and eff_non_returnable:
        raise HTTPException(422, detail={"error": "invalid_billing",
                                         "message": "returnable and non_returnable cannot both be selected"})
    if eff_paid and not (eff_amount and float(eff_amount) > 0):
        raise HTTPException(422, detail={"error": "invalid_billing",
                                         "message": "amount is required and must be greater than 0 when paid"})
    if not eff_paid and eff_amount and float(eff_amount) > 0:
        raise HTTPException(422, detail={"error": "invalid_billing",
                                         "message": "amount must be 0 unless paid"})
    # Re-pointing the requestor moves BOTH the requestor and the BASIS approval binding to
    # the new BH, and mirrors their name into requestor_team. Validated the same way as on
    # create. The sales POC is independent of this and only moves when the patch names one
    # (see below).
    new_bh_uid = payload.get("requestor_user_id")
    new_requestor_team = payload.get("requestor_team")
    if new_bh_uid is not None:
        new_requestor_team = await _assert_business_head(conn, new_bh_uid)
    # Sales POC is editable. Only re-resolve when the patch actually names one — passing
    # the signed-in user as a default here would silently steal the POC from whoever was
    # set at creation every time an unrelated field is edited by someone else.
    poc_touched = any(payload.get(k) is not None
                      for k in ("sales_poc_user_id", "sales_poc_name", "sales_poc_email"))
    new_poc = await _resolve_sales_poc(conn, payload, user) if poc_touched else (None, None, None)
    async with conn.transaction():
        await conn.execute(
            """
            UPDATE sample_requisitions
               SET requestor_team   = COALESCE($2, requestor_team),
                   requestor_user_id     = COALESCE($26, requestor_user_id),
                   business_head_user_id = COALESCE($26, business_head_user_id),
                   purpose_tag      = COALESCE($3, purpose_tag),
                   purpose_note     = COALESCE($4, purpose_note),
                   base_bom_id      = COALESCE($5, base_bom_id),
                   transporter_name = COALESCE($7, transporter_name),
                   vehicle_number   = COALESCE($8, vehicle_number),
                   quantity         = COALESCE($9, quantity),
                   npd_target_name  = COALESCE($10, npd_target_name),
                   warehouse        = COALESCE($11, warehouse),
                   description      = COALESCE($12, description),
                   company_name             = COALESCE($13, company_name),
                   customer_name            = COALESCE($14, customer_name),
                   customer_contact         = COALESCE($15, customer_contact),
                   customer_ship_to_address = COALESCE($16, customer_ship_to_address),
                   mode_of_transport        = COALESCE($17, mode_of_transport),
                   expected_dispatch_date   = COALESCE($18, expected_dispatch_date),
                   confirmed_dispatch_date  = COALESCE($19, confirmed_dispatch_date),
                   pcs              = COALESCE($20, pcs),
                   weight_per_piece = COALESCE($21, weight_per_piece),
                   returnable       = COALESCE($22, returnable),
                   non_returnable   = COALESCE($23, non_returnable),
                   paid             = COALESCE($24, paid),
                   amount           = COALESCE($25, amount),
                   updated_at = NOW(), updated_by = $6
             WHERE id = $1
            """,
            req_id, new_requestor_team, payload.get("purpose_tag"),
            payload.get("purpose_note"), payload.get("base_bom_id"), user.user_id,
            payload.get("transporter_name"), payload.get("vehicle_number"),
            quantity, payload.get("npd_target_name"), new_warehouse,
            payload.get("description"),
            payload.get("company_name"), payload.get("customer_name"),
            payload.get("customer_contact"), payload.get("customer_ship_to_address"),
            payload.get("mode_of_transport"), payload.get("expected_dispatch_date"),
            payload.get("confirmed_dispatch_date"),
            payload.get("pcs"), payload.get("weight_per_piece"),
            payload.get("returnable"), payload.get("non_returnable"), payload.get("paid"),
            # paid explicitly unticked → force amount 0 (keeps the DB CHECK satisfied
            # even if the client omitted amount); else use the sent amount (or keep).
            (0 if payload.get("paid") is False else payload.get("amount")),
            new_bh_uid)
        # Separate + guarded, for the same reason as on create: 085 is hand-applied, so the
        # statement above must stay valid on a database that has not seen it yet.
        if poc_touched and await has_sales_poc_columns(conn):
            await conn.execute(
                """UPDATE sample_requisitions
                      SET sales_poc_user_id = $2, sales_poc_name = $3, sales_poc_email = $4
                    WHERE id = $1""", req_id, *new_poc)
        if payload.get("articles") is not None:
            await conn.execute(
                "DELETE FROM sample_requisition_articles WHERE requisition_id = $1", req_id)
            await _insert_articles(conn, req_id, payload["articles"])
        if targets is not None:
            await _insert_npd_targets(conn, req_id, targets, replace=True)
        elif any(payload.get(k) is not None for k in ("npd_target_name", "pcs", "weight_per_piece")):
            # Header-only target edit (no `targets` sent) — keep child target #1 in sync.
            await _sync_first_npd_target(conn, req_id)
        await audit_service.write_audit(
            conn, req_id, audit_service.EV_ARTICLE_EDIT,
            actor_user_id=user.user_id, actor_role=user.role_name,
            remarks="Requisition edited")

    # If the edited request is already in the NPD reviewers' queue (SUBMITTED /
    # ON_HOLD), re-notify them over WhatsApp with the updated details so they can
    # re-decide. Best-effort, after commit — never blocks the edit. Edits while
    # still DRAFT (not yet sent to NPD) don't notify. req["status"] is pre-update;
    # the PATCH never changes status, so it still reflects the review state.
    if req["sample_type"] in ("NPD", "TRIAL") and req["status"] in ("SUBMITTED", "ON_HOLD"):
        try:
            from app.modules.sample.services import whatsapp_service as wa
            fresh = await _fetch_req(conn, req_id)
            await wa.notify_npd_updated(conn, fresh or req)
        except Exception:  # noqa: BLE001
            logger.exception("WhatsApp NPD updated notify failed for req %s", req_id)

    return await get_requisition(conn, req_id)


async def submit_requisition(conn, req_id: int, *, user) -> dict:
    """DRAFT|BH_REJECTED -> SUBMITTED with validation guards (spec §8)."""
    req = _require(await _fetch_req(conn, req_id), req_id)
    _assert_transition(req["status"], "SUBMITTED")

    # NPD / TRIAL requests are a pure ask — they name the target article only;
    # the NPD team authors the recipe (articles/BOM) later. Article lines are
    # therefore required only for the issuance flows (Basis RM/FG, Internal).
    if req["sample_type"] not in ("NPD", "TRIAL"):
        n_articles = await conn.fetchval(
            "SELECT COUNT(*) FROM sample_requisition_articles WHERE requisition_id = $1", req_id)
        if not n_articles:
            raise HTTPException(422, detail={
                "error": "no_articles",
                "message": "A requisition needs at least one article before submission",
                "details": {"id": req_id}})

    async with conn.transaction():
        await conn.execute(
            "UPDATE sample_requisitions SET status='SUBMITTED', updated_at=NOW(), updated_by=$2 WHERE id=$1",
            req_id, user.user_id)
        await audit_service.write_audit(
            conn, req_id, audit_service.EV_STATUS_CHANGE,
            old_value={"status": req["status"]}, new_value={"status": "SUBMITTED"},
            actor_user_id=user.user_id, actor_role=user.role_name,
            remarks="Submitted for BH approval")
        # Path A handoff: a raised NPD / TRIAL request is the business team asking
        # NPD to develop an article — alert the NPD team so they can pick it up
        # (recipe authoring is allowed pre-approval; promotion still needs the BH
        # gate). Other sample types route through the approval-stage alerts only.
        if req["sample_type"] in ("NPD", "TRIAL"):
            tgt = req.get("npd_target_name")
            await notification_service.emit_alert(
                conn, alert_type="sample_npd_requested",
                target_team=notification_service.TEAM_NPD,
                message=(f"New {req['sample_type']} request {req['request_id']} "
                         f"raised for development" + (f": {tgt}." if tgt else ".")),
                related_id=req_id)

    # WhatsApp the NPD reviewers (best-effort, after commit) so they can accept /
    # hold straight from WhatsApp — the hold reason is captured from their reply.
    if req["sample_type"] in ("NPD", "TRIAL"):
        try:
            from app.modules.sample.services import whatsapp_service as wa
            await wa.notify_npd_review(conn, req)
        except Exception:  # noqa: BLE001
            logger.exception("WhatsApp NPD review notify failed for req %s", req_id)
        try:
            from app.modules.sample.services import sample_mail_service as mail
            await mail.notify_npd_review_email(conn, req)
        except Exception:  # noqa: BLE001
            logger.exception("Sample review email failed for req %s", req_id)
    else:
        # The general sample flow (BASIS_RM / BASIS_FG / INTERNAL) has no NPD review step —
        # it goes straight to the business head. Post the submission into the trail so the
        # BH and inventory see it coming; NPD types get the buttoned review mail above
        # instead, which already announces the same thing.
        try:
            from app.modules.sample.services import sample_mail_service as mail
            await mail.notify_requisition_event(conn, await get_requisition(conn, req_id),
                                                event="submitted")
        except Exception:  # noqa: BLE001
            logger.exception("Sample submitted email failed for req %s", req_id)

    return await get_requisition(conn, req_id)


async def cancel_requisition(conn, req_id: int, *, reason: str, user) -> dict:
    """Any non-terminal status -> CANCELLED (requires a reason, spec §8)."""
    req = _require(await _fetch_req(conn, req_id), req_id)
    _assert_transition(req["status"], "CANCELLED")
    if not (reason or "").strip():
        raise HTTPException(422, detail={
            "error": "reason_required",
            "message": "cancellation_reason is required",
            "details": {"id": req_id}})
    async with conn.transaction():
        await conn.execute(
            """UPDATE sample_requisitions
                  SET status='CANCELLED', cancellation_reason=$2,
                      updated_at=NOW(), updated_by=$3
                WHERE id=$1""",
            req_id, reason, user.user_id)
        await audit_service.write_audit(
            conn, req_id, audit_service.EV_CANCEL,
            old_value={"status": req["status"]}, new_value={"status": "CANCELLED"},
            actor_user_id=user.user_id, actor_role=user.role_name, remarks=reason)
    return await get_requisition(conn, req_id)


async def transition_status(conn, req_id: int, *, target: str, user,
                            remarks: str | None = None,
                            extra: dict | None = None) -> dict:
    """Guarded status move used by sibling services (approval/outward/etc.).

    `extra` is an optional dict of additional column=value updates applied in
    the same statement (e.g. {"linked_job_card_id": 42}).
    """
    req = _require(await _fetch_req(conn, req_id), req_id)
    _assert_transition(req["status"], target)
    sets = ["status = $2", "updated_at = NOW()", "updated_by = $3"]
    params: list = [req_id, target, user.user_id]
    for col, val in (extra or {}).items():
        params.append(val)
        sets.append(f"{col} = ${len(params)}")
    await conn.execute(
        f"UPDATE sample_requisitions SET {', '.join(sets)} WHERE id = $1", *params)
    await audit_service.write_audit(
        conn, req_id, audit_service.EV_STATUS_CHANGE,
        old_value={"status": req["status"]}, new_value={"status": target},
        actor_user_id=user.user_id, actor_role=user.role_name, remarks=remarks)
    return _require(await _fetch_req(conn, req_id), req_id)


async def close_requisition(conn, req_id: int, *, user, remarks: str | None = None) -> dict:
    """Close a dispatched/issued requisition (-> CLOSED). Allowed from
    GATE_PASS_ISSUED, INTERNALLY_DISPATCHED, PARTIALLY_CONVERTED (spec §8)."""
    req = _require(await _fetch_req(conn, req_id), req_id)
    async with conn.transaction():
        await transition_status(conn, req_id, target="CLOSED", user=user,
                                remarks=remarks or "Closed")
    return await get_requisition(conn, req_id)
