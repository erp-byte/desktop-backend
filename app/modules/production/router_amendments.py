"""R8 maker-checker endpoints (B11).

Mounted at /api/v1/production/amendments. Five endpoints:

    POST   /amendments                  - create (maker)
    GET    /amendments                  - list (with ?mine, ?status, etc.)
    POST   /amendments/{id}/approve     - approve (checker)
    POST   /amendments/{id}/reject      - reject (checker)
    POST   /amendments/{id}/withdraw    - withdraw (maker)

The router is intentionally light - all the lifecycle / apply / role-
matrix logic lives in services/amendments_v2.py. Auth uses
get_current_user; the role-matrix gate is enforced by checking
user.role_name against the matrix in this router, NOT in the service
(service stays HTTP-agnostic).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.modules.auth.middleware import get_current_user
from app.modules.production.services.amendments_v2 import (
    REQUEST_TYPES,
    _AmendmentApplyError,
    create_amendment, list_amendments,
    approve_amendment, reject_amendment, withdraw_amendment,
)


router = APIRouter(prefix="/api/v1/production/amendments", tags=["Amendments"])


# ---------------------------------------------------------------------------
# Role matrix (per the R8 framework spec, mapped to seeded roles).
# Maker roles can create; checker roles can approve. Final-checker roles
# are listed for two-checker request types so that the second approval
# can route correctly. All admins can do everything.
# ---------------------------------------------------------------------------

_MAKER_ROLES = {
    'one_off_material_add':       {'team_leader'},
    'one_off_material_remove':    {'team_leader'},
    'permanent_bom_add':          {'floor_manager'},
    'permanent_bom_remove':       {'floor_manager'},
    'permanent_bom_qty_change':   {'floor_manager'},
    'plan_delete':                {'planner'},
    'plan_field_change':          {'planner', 'floor_manager'},
    'stop_process':               {'floor_manager'},
    'force_unlock':               {'floor_manager'},
    'uom_correction':             {'planner'},
    'ncr_disposition':            {'qc_inspector'},
    'unbalanced_close_override':  {'floor_manager'},
    'ega_override':               {'team_leader'},
}

_CHECKER_ROLES_FIRST = {
    'one_off_material_add':       {'floor_manager'},
    'one_off_material_remove':    {'floor_manager'},
    'permanent_bom_add':          {'planner'},
    'permanent_bom_remove':       {'planner'},
    'permanent_bom_qty_change':   {'planner'},
    'plan_delete':                {'admin', 'production_manager'},
    'plan_field_change':          {'admin', 'production_manager'},
    'stop_process':               {'admin', 'production_manager'},
    'force_unlock':               {'admin'},
    'uom_correction':             {'admin'},
    'ncr_disposition':            {'floor_manager'},
    'unbalanced_close_override':  {'admin', 'production_manager'},
    'ega_override':               {'floor_manager'},
}

_CHECKER_ROLES_SECOND = {
    'permanent_bom_add':          {'admin'},
    'permanent_bom_remove':       {'admin'},
    'permanent_bom_qty_change':   {'admin'},
}


def _has_role(user, allowed: set[str]) -> bool:
    return (getattr(user, "is_admin", False)
            or user.role_name in allowed)


# ---------------------------------------------------------------------------
# Pydantic
# ---------------------------------------------------------------------------

class AmendmentCreateRequest(BaseModel):
    request_type: str
    payload:      dict
    job_card_id:  int | None = None
    bom_id:       int | None = None
    reason:       str | None = Field(default=None, max_length=2000)


class AmendmentApproveRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class AmendmentRejectRequest(BaseModel):
    rejection_reason: str = Field(min_length=1, max_length=2000)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("")
async def create(
    request: Request,
    body: AmendmentCreateRequest,
    user=Depends(get_current_user),
):
    if body.request_type not in REQUEST_TYPES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_request_type",
                "request_type": body.request_type,
                "allowed": sorted(REQUEST_TYPES),
            },
        )
    # Role gate: caller must be a valid maker for this request type.
    allowed_makers = _MAKER_ROLES.get(body.request_type, set())
    if not _has_role(user, allowed_makers):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "not_authorised_maker",
                "request_type": body.request_type,
                "your_role": user.role_name,
                "allowed_maker_roles": sorted(allowed_makers),
            },
        )
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await create_amendment(
                conn,
                request_type=body.request_type,
                payload=body.payload,
                job_card_id=body.job_card_id,
                bom_id=body.bom_id,
                reason=body.reason,
                maker_user_id=user.user_id,
            )
    if result.get("error") in ("invalid_request_type", "invalid_payload",
                                "payload_missing_key", "payload_invalid_value",
                                "maker_unknown"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.get("")
async def list_(
    request: Request,
    status:       str | None = Query(None, description="comma-separated"),
    request_type: str | None = Query(None),
    job_card_id:  int | None = Query(None),
    bom_id:       int | None = Query(None),
    mine:         bool = Query(False, description="Limit to rows where caller is maker / checker"),
    page:         int = Query(1, ge=1),
    page_size:    int = Query(50, ge=1, le=500),
    user=Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await list_amendments(
            conn,
            status=status, request_type=request_type,
            job_card_id=job_card_id, bom_id=bom_id,
            mine_user_id=user.user_id if mine else None,
            page=page, page_size=page_size,
        )


@router.post("/{request_id}/approve")
async def approve(
    request: Request,
    request_id: int,
    body: AmendmentApproveRequest | None = None,
    user=Depends(get_current_user),
):
    body = body or AmendmentApproveRequest()
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                # H2: read the row INSIDE the txn with FOR UPDATE so the
                # role-matrix check sees the same status that
                # approve_amendment will act on (eliminates the
                # toctou window where a racing approve flipped
                # pending_review -> pending_final between the role
                # lookup and the service call).
                row = await conn.fetchrow(
                    "SELECT request_type, status, maker_user_id, "
                    "       checker1_user_id "
                    "FROM   bom_amendment_request_v2 "
                    "WHERE  request_id=$1 "
                    "FOR    UPDATE",
                    request_id,
                )
                if row is None:
                    raise HTTPException(status_code=404, detail="Amendment not found")
                rt = row["request_type"]
                if row["status"] == 'pending_review':
                    expected = _CHECKER_ROLES_FIRST.get(rt, set())
                    slot = "first"
                elif row["status"] == 'pending_final':
                    expected = _CHECKER_ROLES_SECOND.get(rt, set())
                    slot = "second"
                else:
                    raise HTTPException(status_code=409, detail={
                        "error": "invalid_state",
                        "status": row["status"],
                    })
                if not _has_role(user, expected):
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "error": "not_authorised_checker",
                            "slot": slot,
                            "request_type": rt,
                            "your_role": user.role_name,
                            "allowed_checker_roles": sorted(expected),
                        },
                    )
                result = await approve_amendment(
                    conn,
                    request_id=request_id,
                    checker_user_id=user.user_id,
                    note=body.note,
                )
        except _AmendmentApplyError as apply_err:
            # C1: the service raised so the txn rolled back. Surface the
            # apply-result dict at HTTP-422 (semantic failure - the
            # approval was valid but the side-effect could not be
            # carried out). Amendment stays at its pre-approve status.
            raise HTTPException(status_code=422, detail={
                "error": "apply_failed",
                "message": (
                    "Approval rolled back: the apply step failed. "
                    "The amendment is unchanged; fix the underlying "
                    "cause and re-approve."
                ),
                "apply_error": apply_err.apply_result,
            })
    if result.get("error") == "amendment_not_found":
        raise HTTPException(status_code=404, detail="Amendment not found")
    if result.get("error") == "maker_unknown":
        raise HTTPException(status_code=409, detail=result)
    if result.get("error") in ("amendment_terminal", "invalid_state",
                                "duplicate_checker", "duplicate_first_checker",
                                "self_approval_forbidden"):
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/{request_id}/reject")
async def reject(
    request: Request,
    request_id: int,
    body: AmendmentRejectRequest,
    user=Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Role gate: any checker on either slot can reject.
            row = await conn.fetchrow(
                "SELECT request_type, status FROM bom_amendment_request_v2 "
                "WHERE  request_id=$1",
                request_id,
            )
            if row is None:
                raise HTTPException(status_code=404, detail="Amendment not found")
            rt = row["request_type"]
            permitted = (_CHECKER_ROLES_FIRST.get(rt, set())
                         | _CHECKER_ROLES_SECOND.get(rt, set()))
            if not _has_role(user, permitted):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": "not_authorised_checker",
                        "request_type": rt,
                        "your_role": user.role_name,
                        "allowed_checker_roles": sorted(permitted),
                    },
                )
            result = await reject_amendment(
                conn,
                request_id=request_id,
                checker_user_id=user.user_id,
                rejection_reason=body.rejection_reason,
            )
    if result.get("error") == "amendment_not_found":
        raise HTTPException(status_code=404, detail="Amendment not found")
    if result.get("error") in ("amendment_terminal", "missing_rejection_reason",
                                "self_approval_forbidden", "maker_unknown"):
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/{request_id}/withdraw")
async def withdraw(
    request: Request,
    request_id: int,
    user=Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await withdraw_amendment(
                conn,
                request_id=request_id,
                maker_user_id=user.user_id,
            )
    if result.get("error") == "amendment_not_found":
        raise HTTPException(status_code=404, detail="Amendment not found")
    if result.get("error") in ("withdraw_not_allowed", "not_maker"):
        raise HTTPException(status_code=409, detail=result)
    return result
