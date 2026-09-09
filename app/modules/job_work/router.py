"""/api/v1/job-work/* — Job Work (material sent out for third-party processing).

Slice 1: POST /out, the Material Out challan. The reference surface is
legacy_backend/services/ims_service/job_work_server.py (18 routes); the rest —
list/detail/search, PUT /out/{id}, the material-in receive flow, the SKU
pickers, the Excel/PDF importers — is not built yet.

Base path follows the rebuild convention (/api/v1/<module>), not the reference's
root-mounted /job-work. The web client already targets it: see
web_replica/src/lib/jobWork.ts.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.modules.auth.middleware import AuthUser, get_current_user
from app.modules.job_work import permissions, schemas
from app.modules.job_work.services import create_service

router = APIRouter(prefix="/api/v1/job-work", tags=["Job Work"])


@router.post("/out", response_model=schemas.JobWorkRecordOut, status_code=201)
async def create_material_out(
    request: Request,
    body: schemas.JobWorkCreateRequest,
    user: AuthUser = Depends(get_current_user),
):
    """Create a Material Out delivery challan and deduct its boxes from cold storage.

    Scanned boxes (box_id + transaction_no + cold_unit all present) are removed
    from cfpl/cdpl_cold_stocks and logged to the disposition ledger. The challan
    and the deduction are one transaction. `cold_boxes_deducted` in the response
    reports how many rows actually left inventory — compare it against the
    number of scanned lines to spot boxes that were already gone.

    Returns **201** with the full record, matching a later GET. Note two
    divergences from the reference, which returned `{status, id, challan_no}`
    with HTTP 200 for every outcome:
      * **409** if the challan number was already submitted (double-submit).
      * **422** if one box appears on two lines of the same challan.
    """
    permissions.assert_can_dispatch_from(user, body.header.from_warehouse)
    # The raw body, not body.model_dump(): jb_materialout_header.payload is the
    # only home for `company` / `totals` / `tax_summary` / per-line `hsn_sac`,
    # and re-serialising the model would drop the fields it does not declare.
    # Starlette caches the body, so this does not re-read the stream.
    raw_payload = await request.json()
    actor = user.email or user.full_name or str(user.user_id)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await create_service.create_out(conn, body, raw_payload, actor)
