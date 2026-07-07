"""/api/v1/packing-details/* — Packing Details module.

CRUD over the ``packing_details`` table. Each row carries a ``batch_code``, an
``article_name``, and a free-form JSONB ``details`` body. The primary key is an
app-supplied 8-digit time-based BIGINT (``new_short_time_id``) minted on create
— the same identifier scheme the job-card v2 module uses.

All endpoints require a valid access token; the caller is recorded as
``created_by`` on create.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.modules.auth.middleware import AuthUser, get_current_user
from app.modules.packing import schemas
from app.modules.packing.services import packing_crypto, packing_service
from app.modules.packing.services.packing_crypto import InvalidBatchToken

router = APIRouter(prefix="/api/v1/packing-details", tags=["Packing Details"])


def _created_by(user: AuthUser) -> str:
    """Human-readable actor label for the created_by column."""
    return user.full_name or user.email or str(user.user_id)


def _not_found(packing_id: int) -> HTTPException:
    # detail keyed on "error"/"message" so request_context surfaces the specific
    # machine code at the envelope's top level (the dominant house convention),
    # not buried under `details`.
    return HTTPException(
        404,
        detail={
            "error": "packing_detail_not_found",
            "message": f"No packing_details row with id {packing_id}",
            "details": {"packing_id": packing_id},
        },
    )


@router.post("", status_code=201, response_model=schemas.PackingDetailOut)
async def create_packing_detail(
    body: schemas.PackingDetailCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        # Outer transaction so insert_with_pk_retry's SAVEPOINT works.
        async with conn.transaction():
            return await packing_service.create_packing_detail(
                conn,
                batch_code=body.batch_code,
                article_name=body.article_name,
                details=body.details,
                created_by=_created_by(user),
            )


# ── encrypted batch-token access ───────────────────────────────────────────
# The batch number travels as an opaque AES-256-GCM ciphertext. `/batch-token`
# mints it (plaintext batch_code -> token); `/by-encrypted-batch` decrypts a
# token server-side and returns the stored packing data for that batch.


@router.post("/batch-token", response_model=schemas.BatchTokenResponse)
async def mint_batch_token(
    body: schemas.BatchTokenMintRequest,
    user: AuthUser = Depends(get_current_user),
):
    """Encrypt a plaintext batch_code into a token the frontend can store."""
    return {"batch_token": packing_crypto.encrypt_batch_code(body.batch_code)}


@router.post("/by-encrypted-batch", response_model=list[schemas.PackingDetailOut])
async def fetch_by_encrypted_batch(
    body: schemas.EncryptedBatchRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    """Decrypt the batch token, then return the stored packing_details rows
    (each carrying its JSON `details` body) for that batch."""
    try:
        batch_code = packing_crypto.decrypt_batch_code(body.batch_token)
    except InvalidBatchToken:
        raise HTTPException(
            400,
            detail={"error": "invalid_batch_token",
                    "message": "The batch token could not be decrypted"},
        )
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await packing_service.list_packing_details(
            conn, batch_code=batch_code, limit=500, offset=0
        )


@router.get("/public/scan")
async def public_scan(
    request: Request,
    t: str = Query(..., min_length=1, description="AES-GCM batch token from a scanned QR"),
):
    """PUBLIC (no auth) — resolve a scanned QR's batch token to its packing
    block details. The encrypted token is the capability: only a token minted
    server-side (POST /batch-token) decrypts, so no bearer auth is required.
    CORS is open (see main.py) so the candorfoods.in Wix page can call this.
    Returns a public-safe shape (omits created_by / internal columns)."""
    try:
        batch_code = packing_crypto.decrypt_batch_code(t)
    except InvalidBatchToken:
        raise HTTPException(
            400,
            detail={"error": "invalid_batch_token",
                    "message": "The batch token could not be decrypted"},
        )
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await packing_service.list_packing_details(
            conn, batch_code=batch_code, limit=500, offset=0
        )
    return {
        "batch_code": batch_code,
        "count": len(rows),
        "records": [
            {
                "packing_id": r["packing_id"],
                "article_name": r["article_name"],
                "details": r["details"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


@router.get("", response_model=list[schemas.PackingDetailOut])
async def list_packing_details(
    request: Request,
    batch_code: Optional[str] = Query(None),
    article_name: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await packing_service.list_packing_details(
            conn,
            batch_code=batch_code,
            article_name=article_name,
            limit=limit,
            offset=offset,
        )


@router.get("/{packing_id}", response_model=schemas.PackingDetailOut)
async def get_packing_detail(
    packing_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await packing_service.get_packing_detail(conn, packing_id)
    if row is None:
        raise _not_found(packing_id)
    return row


@router.patch("/{packing_id}", response_model=schemas.PackingDetailOut)
async def update_packing_detail(
    packing_id: int,
    body: schemas.PackingDetailUpdate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    provided = body.model_dump(exclude_unset=True)
    if not provided:
        raise HTTPException(
            400,
            detail={"error": "empty_update",
                    "message": "Provide at least one field to update"},
        )

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await packing_service.update_packing_detail(
                conn,
                packing_id,
                batch_code=body.batch_code,
                article_name=body.article_name,
                details=body.details,
                details_provided="details" in provided,
            )
    if row is None:
        raise _not_found(packing_id)
    return row


@router.delete("/{packing_id}", status_code=204)
async def delete_packing_detail(
    packing_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        deleted = await packing_service.delete_packing_detail(conn, packing_id)
    if not deleted:
        raise _not_found(packing_id)
    # 204 No Content — no body returned.
