"""BOM data-quality audit endpoints.

Mounted at /api/v1/production/audit. Surfaces master-data inconsistencies
that runtime code cannot programmatically correct — operators triage the
findings and lodge R8 amendments to fix the underlying rows.

Currently a single endpoint:

    GET /api/v1/production/audit/bom-qpu

Lists every active `bom_line` whose `item_type='pm'` AND whose FG SKU's
`all_sku.uom` is not 1.000 — i.e. per-piece FG SKUs whose PM BOM lines
are at risk of having been entered with `quantity_per_unit` expressed as
"per FG piece" instead of the canonical "per kg of FG" convention.

Background: the canonical BOM convention (job_card_v2.py:225-227, the
indent generator, and jc_accounting_v2.py:170-173, the variance calc)
is `reqd = planned_qty_kg × quantity_per_unit`. That convention is
correct ONLY when `quantity_per_unit` is expressed per kg of FG. For a
500 gm pouch SKU (`all_sku.uom = 0.500`), a PM line erroneously entered
with `quantity_per_unit = 1.0` ("one pouch per finished piece") produces
a BOM prescribed qty that is exactly `all_sku.uom` (= 0.5) times what it
should be. The variance chip then shows the operator a fictional
+100 % over-consumption when the JC is actually balanced.

The endpoint does NOT mutate the BOM master. It returns the suspect
rows + both interpretations (current vs per-piece-intent) so the
operator can decide which is correct and lodge a `permanent_bom_qty_change`
amendment via R8.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.modules.auth.middleware import get_current_user


router = APIRouter(prefix="/api/v1/production/audit", tags=["Audit"])


# Roles permitted to view the audit. Admins are always allowed. Planner +
# floor_manager can audit because they are the makers/checkers on the
# downstream `permanent_bom_qty_change` amendment that fixes a flagged row.
_AUDIT_ROLES = {"admin", "planner", "floor_manager", "plant_manager"}


def _role_name(user) -> str | None:
    return getattr(user, "role_name", None) or (
        user.get("role_name") if isinstance(user, dict) else None
    )


def _is_admin(user) -> bool:
    val = getattr(user, "is_admin", None)
    if val is None and isinstance(user, dict):
        val = user.get("is_admin")
    return bool(val)


def _has_audit_role(user) -> bool:
    if _is_admin(user):
        return True
    rn = (_role_name(user) or "").lower()
    return rn in _AUDIT_ROLES


@router.get("/bom-qpu")
async def bom_qpu_audit(
    request: Request,
    bom_id:           int | None  = Query(None, description="restrict to one BOM"),
    fg_sku_name:      str | None  = Query(None, description="restrict to one FG SKU (exact match)"),
    only_suspicious:  bool        = Query(True, description="hide PM lines whose FG SKU uom is 1.000 (no risk)"),
    active_only:      bool        = Query(True, description="only is_active BOM headers"),
    limit:            int         = Query(500, ge=1, le=5000),
    user=Depends(get_current_user),
):
    """Surface PM bom_line rows whose `quantity_per_unit` may have been
    entered as 'per FG piece' instead of 'per kg of FG'.

    Returns each suspect row with the current qpu AND the qpu that would
    be canonical if the entry was really per-piece — so operators can pick
    the right value before lodging a `permanent_bom_qty_change` amendment.
    """
    if not _has_audit_role(user):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "not_authorised",
                "your_role": _role_name(user),
                "allowed_roles": sorted(_AUDIT_ROLES),
                "message": "Only admin / planner / floor_manager / plant_manager can run this audit.",
            },
        )

    where: list[str] = ["bl.item_type = 'pm'"]
    params: list = []
    if bom_id is not None:
        params.append(bom_id)
        where.append(f"bh.bom_id = ${len(params)}")
    if fg_sku_name:
        params.append(fg_sku_name)
        where.append(f"bh.fg_sku_name = ${len(params)}")
    if active_only:
        where.append("bh.is_active = TRUE")
    if only_suspicious:
        # FG SKU's per-unit kg is set AND not 1.000 — there is a non-trivial
        # difference between 'per kg of FG' and 'per FG piece'.
        where.append("asku.uom IS NOT NULL")
        where.append("asku.uom > 0")
        where.append("asku.uom <> 1.000")

    sql = f"""
        SELECT
            bh.bom_id,
            bh.fg_sku_name,
            bh.version            AS bom_version,
            bh.is_active          AS bom_is_active,
            bh.pack_size_kg       AS bom_pack_size_kg,
            asku.uom              AS fg_sku_uom_kg,
            bl.bom_line_id,
            bl.line_number,
            bl.material_sku_name,
            bl.item_type,
            bl.uom                AS qpu_uom,
            bl.quantity_per_unit  AS current_qpu,
            bl.loss_pct
        FROM   bom_header bh
        JOIN   bom_line   bl    ON bl.bom_id = bh.bom_id
        LEFT  JOIN all_sku asku ON asku.particulars = bh.fg_sku_name
        WHERE  {' AND '.join(where)}
        ORDER  BY bh.fg_sku_name, bh.bom_id, bl.line_number
        LIMIT  {limit}
    """

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    out_rows: list[dict] = []
    distinct_boms: set[int] = set()
    distinct_skus: set[str] = set()
    suspicious_count = 0

    for r in rows:
        fg_uom = float(r["fg_sku_uom_kg"]) if r["fg_sku_uom_kg"] is not None else None
        cur_qpu = float(r["current_qpu"]) if r["current_qpu"] is not None else None

        # Per-piece interpretation: if the entered qpu was actually per FG
        # piece, then the canonical (per kg of FG) qpu is qpu / fg_uom_kg.
        if_per_piece_qpu_per_kg: float | None = None
        if cur_qpu is not None and fg_uom is not None and fg_uom > 0:
            if_per_piece_qpu_per_kg = round(cur_qpu / fg_uom, 6)

        # Heuristic confidence — qpu close to a clean simple ratio that
        # also has fg_uom != 1.000 is the strongest signal of per-piece
        # intent. Anyone reading "qpu = 1.000" on a 500 gm SKU should
        # almost certainly read it as 1 PM per FG piece.
        clean_signal: str | None = None
        if cur_qpu is not None:
            if abs(cur_qpu - round(cur_qpu)) < 0.001 and round(cur_qpu) >= 1:
                clean_signal = f"qpu ≈ {int(round(cur_qpu))} (clean integer — likely per FG piece)"
            else:
                # Check 1/N where N is a small integer (e.g. 1/15 = 0.0667).
                for n in range(2, 101):
                    if abs(cur_qpu - 1 / n) < 0.001:
                        clean_signal = f"qpu ≈ 1/{n} (likely '1 PM per {n} FG pieces')"
                        break

        is_suspicious = (
            fg_uom is not None and fg_uom > 0 and abs(fg_uom - 1.0) > 1e-6
            and cur_qpu is not None
        )
        if is_suspicious:
            suspicious_count += 1

        distinct_boms.add(int(r["bom_id"]))
        if r["fg_sku_name"]:
            distinct_skus.add(r["fg_sku_name"])

        out_rows.append({
            "bom_id":                       int(r["bom_id"]),
            "bom_version":                  r["bom_version"],
            "bom_is_active":                bool(r["bom_is_active"]) if r["bom_is_active"] is not None else None,
            "bom_pack_size_kg":             float(r["bom_pack_size_kg"]) if r["bom_pack_size_kg"] is not None else None,
            "fg_sku_name":                  r["fg_sku_name"],
            "fg_sku_uom_kg":                fg_uom,
            "bom_line_id":                  int(r["bom_line_id"]) if r["bom_line_id"] is not None else None,
            "line_number":                  r["line_number"],
            "material_sku_name":            r["material_sku_name"],
            "item_type":                    r["item_type"],
            "qpu_uom":                      r["qpu_uom"],
            "current_qpu":                  cur_qpu,
            "loss_pct":                     float(r["loss_pct"]) if r["loss_pct"] is not None else None,
            # Two interpretations:
            #   A. Runtime treats current_qpu AS per kg of FG  (canonical).
            #   B. If entry was actually per FG piece, then the canonical
            #      qpu (per kg of FG) is current_qpu / fg_sku_uom_kg.
            "if_per_piece_actual_qpu_per_kg": if_per_piece_qpu_per_kg,
            "qpu_clean_signal":             clean_signal,
            "is_suspicious":                is_suspicious,
            "reason_flagged":               (
                "PM line on a per-piece FG SKU (all_sku.uom ≠ 1.000). "
                "Verify quantity_per_unit was entered as 'per kg of FG' "
                "(R2 canonical convention) and not 'per FG piece'. If "
                "the latter, lodge a permanent_bom_qty_change amendment "
                "setting qpu = if_per_piece_actual_qpu_per_kg."
            ) if is_suspicious else None,
        })

    return {
        "rows":     out_rows,
        "summary": {
            "rows_returned":      len(out_rows),
            "suspicious_rows":    suspicious_count,
            "distinct_boms":      len(distinct_boms),
            "distinct_fg_skus":   len(distinct_skus),
            "limit_applied":      limit,
            "limit_reached":      len(out_rows) >= limit,
        },
        "convention": (
            "bom_line.quantity_per_unit is 'per kg of FG' for both RM and PM "
            "(see job_card_v2.py:225-227 + jc_accounting_v2.py:170-173). "
            "Variance and indent math both multiply qpu × planned_qty_kg."
        ),
    }
