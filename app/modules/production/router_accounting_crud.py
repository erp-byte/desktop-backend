"""Accounting CRUD endpoints — one record per (job card, batch).

    GET    /api/v1/production/accounting/record
    POST   /api/v1/production/accounting/record
    PUT    /api/v1/production/accounting/record
    DELETE /api/v1/production/accounting/record   (soft)

These replace the composite trio the Accounting tab used to call
(POST /job-cards-v2/{id}/outputs, PUT .../accounting/summary,
GET .../accounting). The old routes stay mounted until the frontend is
rewired — removing them in the same change would break a live tab.

All four take the same three 8-digit identifiers. job_card_id + batch_id
resolve the record; plan_id is a GUARD, validated against job_card_v2.plan_id
so a mismatched triple 409s instead of quietly editing a different card.

Identifiers are passed as QUERY params on all four verbs, including PUT, so one
URL identifies one record regardless of method — the body carries data only,
never identity. That keeps GET/PUT/DELETE addressable by the same link.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from app.modules.auth.middleware import require_permission
from app.modules.production.services import jc_accounting_crud as svc

router = APIRouter(prefix="/api/v1/production/accounting", tags=["Accounting CRUD"])


# ---------------------------------------------------------------------------
# Payload models — the field list the Accounting tab already sends.
# ---------------------------------------------------------------------------

class ConsumedLine(BaseModel):
    bom_line_id: int | None = None
    material_sku_name: str
    consumed_qty: float = 0.0
    input_kind: str | None = None
    source_dispatch_id: int | None = None
    remarks: str | None = None


class ByproductLine(BaseModel):
    category: str
    qty_kg: float = 0.0
    uom: str = "KGS"
    material_name: str | None = None
    bom_line_id: int | None = None
    remarks: str | None = None


class BalanceMaterialLine(BaseModel):
    material_name: str | None = None
    balance_type: str
    qty_kg: float = 0.0
    # Unit for qty_kg (migration 094). Nullable on purpose: a missing unit
    # reads as unknown, which is honest, where defaulting to KGS would
    # relabel a PM line counted in pieces as a weight.
    uom: str | None = None
    bom_line_id: int | None = None
    material_id: int | None = None
    remarks: str | None = None

    @field_validator("balance_type")
    @classmethod
    def _known_type(cls, v):
        allowed = ("extra_given", "returned", "wastage", "control_sample")
        if v not in allowed:
            raise ValueError(f"balance_type must be one of {allowed}")
        return v


class AdditiveLine(BaseModel):
    sku_name: str | None = None
    material_name: str | None = None
    qty_kg: float = 0.0
    uom: str | None = None      # see BalanceMaterialLine.uom (migration 094)
    remarks: str | None = None


class QCBlock(BaseModel):
    passed: bool
    remarks: str | None = None
    corrective_action: str | None = None
    inspector: str | None = None


class AccountingRecordBody(BaseModel):
    """Body for POST and PUT.

    PUT is a FULL-RECORD replace: an omitted array means "this record has no
    lines of that kind", and its stored lines are soft-deleted. That is
    deliberately NOT the old POST /outputs convention (where None meant "leave
    this section untouched") — for CRUD, the body IS the record.
    """
    output_qty_kg: float = 0.0
    output_qty_units: float | None = None
    output_kind: str | None = None
    uom: str | None = None
    rm_consumed_kg: float = 0.0
    process_loss_kg: float = 0.0
    process_loss_remark: str | None = None
    notes: str | None = None

    rm_consumed: list[ConsumedLine] = Field(default_factory=list)
    pm_consumed: list[ConsumedLine] = Field(default_factory=list)
    byproducts: list[ByproductLine] = Field(default_factory=list)
    balance_materials: list[BalanceMaterialLine] = Field(default_factory=list)
    additives: list[AdditiveLine] = Field(default_factory=list)
    qc: QCBlock | None = None

    # Control flag, never persisted: permits writing against a closed /
    # cancelled batch, or deleting a completed JC's record. Honoured only for
    # admins — a non-admin sending it gets a 403.
    admin_override: bool = False


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------

def _ids(
    job_card_id: int = Query(..., gt=0, description="job_card_id (short-time id)"),
    plan_id: int = Query(..., gt=0, description="plan_id — guard, must match the JC"),
    batch_id: int = Query(..., gt=0, description="batch_id (short-time id)"),
) -> dict:
    """The three identifiers, as query params on all four verbs.

    NOT width-validated. These are usually 8 digits because new_short_time_id()
    mints them that way, but "8-digit" is a description of the generator, not an
    invariant of the data: job_card_batch_v2 already holds batch_id=1200019
    (7 digits). A ge=10_000_000 check would have 404'd that batch forever, so the
    only constraint enforced here is > 0 — real existence is checked against the
    database by _resolve(), which is the honest test anyway.
    """
    return {"job_card_id": job_card_id, "plan_id": plan_id, "batch_id": batch_id}


# error -> HTTP status. Anything unmapped falls through to 400.
_STATUS = {
    "job_card_not_found": 404,
    "batch_not_found": 404,
    "record_not_found": 404,
    "plan_mismatch": 409,
    "batch_mismatch": 409,
    "record_exists": 409,
    "locked": 409,
    "batch_not_open": 409,
    "batch_cancelled": 409,
    "job_card_closed": 409,
    "duplicate_line": 400,
    "invalid_line": 400,
    "summary_failed": 500,
}


def _raise(result: dict) -> None:
    err = result.get("error") if result else None
    if err:
        raise HTTPException(status_code=_STATUS.get(err, 400), detail=result)


def _guard_override(body_override: bool, user) -> bool:
    """admin_override is admin-only; a non-admin sending it is an error, not a
    silently-ignored flag — otherwise the caller believes it took effect."""
    if not body_override:
        return False
    if not bool(getattr(user, "is_admin", False)):
        raise HTTPException(
            status_code=403,
            detail={"error": "admin_override_forbidden",
                    "message": "admin_override requires an admin account."},
        )
    return True


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/record")
async def view_accounting_record(
    request: Request,
    ids: dict = Depends(_ids),
    user=Depends(require_permission("production", "job_cards", "accounting",
                                    action="view")),
):
    """Read the accounting record for one batch. Live (non-deleted) rows only."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await svc.get_record(conn, **ids)
    _raise(result)
    return result


@router.post("/record", status_code=201)
async def create_accounting_record(
    request: Request,
    body: AccountingRecordBody,
    ids: dict = Depends(_ids),
    user=Depends(require_permission("production", "job_cards", "accounting",
                                    action="close")),
):
    """Create the record for a batch. 409 if one already exists — use PUT."""
    override = _guard_override(body.admin_override, user)
    pool = request.app.state.db_pool
    payload = body.model_dump()
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await svc.create_record(
                conn, **ids, payload=payload,
                actor=user.full_name or user.phone, admin_override=override,
            )
    _raise(result)
    return result


@router.put("/record")
async def update_accounting_record(
    request: Request,
    body: AccountingRecordBody,
    ids: dict = Depends(_ids),
    user=Depends(require_permission("production", "job_cards", "accounting",
                                    action="close")),
):
    """Update the record, writing only the fields that actually differ.

    The service fetches the stored record, compares field by field, and issues
    one UPDATE per changed scalar / line. Untouched lines are not rewritten, so
    the JC edit log stays a record of real operator edits.
    """
    override = _guard_override(body.admin_override, user)
    pool = request.app.state.db_pool
    payload = body.model_dump()
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await svc.update_record(
                conn, **ids, payload=payload,
                actor=user.full_name or user.phone, admin_override=override,
            )
    _raise(result)
    return result


@router.delete("/record")
async def delete_accounting_record(
    request: Request,
    ids: dict = Depends(_ids),
    admin_override: bool = Query(False,
                                 description="Admin-only: allow deleting a "
                                             "completed/closed JC's record"),
    user=Depends(require_permission("production", "job_cards", "accounting",
                                    action="close")),
):
    """Soft-delete the record, then recompute the balance summary.

    Nothing is physically removed — every row keeps its data and gains
    deleted_at / deleted_by. The summary row is recomputed rather than deleted
    so job_card_accounting_v2 always describes rows that still exist.

    Refused on a completed / closed JC without admin_override: an emptied record
    computes total_input = 0, which puts the balance check on its
    absolute-tolerance branch and would read as "balanced" — leaving a closed
    job card whose figures are gone but whose close gate is still satisfied.
    """
    override = _guard_override(admin_override, user)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await svc.delete_record(
                conn, **ids, actor=user.full_name or user.phone,
                admin_override=override,
            )
    _raise(result)
    return result
