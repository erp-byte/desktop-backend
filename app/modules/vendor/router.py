"""FastAPI router for /api/v1/vendors/*.

Endpoint groups:
    vendor_master:  POST / GET (list+one) / PATCH / DELETE (soft)
    vendor_banking: nested under /{vendor_id}/banking — CRUD + set-primary
    vendor_document: manual create, /extract (dry-run), /upload-and-save,
                     append-file, CRUD
    vendor_contract: same pattern as document

Auth: every handler requires a valid JWT; permission strings follow the
module/sub_module convention used by ncr/po routers — `vendor` is the
module, the sub_module names the table family. Admin bypasses checks.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import ValidationError

from app.modules.auth.middleware import AuthUser, require_permission
from app.modules.auth.services.permission_service import check_permission

# Hard cap on files-per-request for /with-documents and /extract-bulk to
# bound memory + total extraction wall-clock. With the parallel
# S3 + Claude fan-out, total time is roughly max(S3, Claude) across the
# batch — typically ~15-25s for 20 files — so 20 stays under the 30s
# soft-ceiling we treat as comfortable for a single HTTP request.
_MAX_DOCS_PER_REQUEST = 20
from app.modules.vendor import storage as vendor_storage
from app.modules.vendor.schemas import (
    BankingCreateRequest,
    BankingResponse,
    BankingUpdateRequest,
    ContractExtractResponse,
    ContractManualCreate,
    ContractResponse,
    ContractUpdate,
    ContractUploadResponse,
    DocExtractResponse,
    DocumentManualCreate,
    DocumentResponse,
    DocumentUpdate,
    DocumentUploadFailure,
    DocumentUploadResponse,
    ExtractBulkResponse,
    HistoryEntry,
    HistoryListResponse,
    HistoryRevertRequest,
    SubmitStagedRequest,
    SubmitStagedResponse,
    VendorCreateRequest,
    VendorDetailResponse,
    VendorListResponse,
    VendorResponse,
    VendorStatus,
    VendorUpdateRequest,
    VendorWithDocumentsResponse,
)
from app.modules.vendor.services import (
    banking_service,
    contract_service,
    document_service,
    extraction_service,
    history_service,
    vendor_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/vendors", tags=["Vendors"])


# ── helpers ──────────────────────────────────────────────────────────────


async def _require_vendor(request: Request, vendor_id: str) -> dict:
    """Fetch vendor or 404. Used by every nested route."""
    pool = request.app.state.db_pool
    vendor = await vendor_service.get_vendor(pool, vendor_id)
    if not vendor:
        raise HTTPException(404, detail={"code": "vendor_not_found", "vendor_id": vendor_id})
    return vendor


def _patch_to_400(e: vendor_service.NotNullPatchError) -> HTTPException:
    """Translate a NOT-NULL-clearing PATCH error into a structured 400."""
    return HTTPException(
        400,
        detail={
            "code": "field_cannot_be_null",
            "field": e.field,
            "msg": f"PATCH cannot set NOT NULL column '{e.field}' to null",
        },
    )


async def _sniff_and_read_upload(
    file: UploadFile,
) -> tuple[bytes, str, str]:
    """Read upload bytes, run magic-byte sniff, reject on size / MIME issues.

    Returns (content, effective_mime, original_filename). Raises 415 / 413 / 400.
    """
    content = await file.read()
    if not content:
        raise HTTPException(400, detail={"code": "empty_file"})
    if len(content) > vendor_storage.MAX_VENDOR_UPLOAD_BYTES:
        raise HTTPException(
            413,
            detail={
                "code": "file_too_large",
                "limit_bytes": vendor_storage.MAX_VENDOR_UPLOAD_BYTES,
                "actual_bytes": len(content),
            },
        )

    sniffed, mismatch = vendor_storage.sniff_mime(content, file.content_type)
    if sniffed not in vendor_storage.ALLOWED_VENDOR_MIMES:
        raise HTTPException(
            415,
            detail={
                "code": "unsupported_media_type",
                "allowed": sorted(vendor_storage.ALLOWED_VENDOR_MIMES),
                "sniffed": sniffed,
                "declared": file.content_type,
            },
        )
    if mismatch:
        raise HTTPException(
            415,
            detail={
                "code": "mime_mismatch",
                "sniffed": sniffed,
                "declared": file.content_type,
            },
        )
    return content, sniffed, file.filename or ""


# ── vendor_master ────────────────────────────────────────────────────────


@router.post("", response_model=VendorResponse, status_code=201)
@router.post("/", response_model=VendorResponse, status_code=201, include_in_schema=False)
async def create_vendor_endpoint(
    request: Request,
    body: VendorCreateRequest,
    user: AuthUser = Depends(require_permission("vendor", "master", action="create")),
):
    row = await vendor_service.create_vendor(
        request.app.state.db_pool,
        body.model_dump(exclude_unset=True),
        actor_user_id=str(user.user_id),
    )
    return VendorResponse.model_validate(row)


@router.get("", response_model=VendorListResponse)
@router.get("/", response_model=VendorListResponse, include_in_schema=False)
async def list_vendors_endpoint(
    request: Request,
    status: VendorStatus | None = Query(
        None,
        description="Filter by vendor status. One of: active, inactive, blacklisted.",
    ),
    category_code_id: str | None = Query(None),
    search: str | None = Query(None, min_length=2),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user: AuthUser = Depends(require_permission("vendor", "master", action="view")),
):
    rows, total = await vendor_service.list_vendors(
        request.app.state.db_pool,
        status=status,
        category_code_id=category_code_id,
        search=search,
        page=page,
        page_size=page_size,
    )
    return VendorListResponse(
        vendors=[VendorResponse.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{vendor_id}", response_model=VendorDetailResponse)
async def get_vendor_endpoint(
    request: Request,
    vendor_id: str,
    user: AuthUser = Depends(require_permission("vendor", "master", action="view")),
):
    pool = request.app.state.db_pool
    vendor = await _require_vendor(request, vendor_id)
    # L3: run the three nested fetches in parallel once the vendor row is
    # confirmed. Cuts detail-GET latency by ~3x for vendors with sub-rows.
    banking, documents, contracts = await asyncio.gather(
        banking_service.list_banking(pool, vendor_id),
        document_service.list_documents(pool, vendor_id),
        contract_service.list_contracts(pool, vendor_id),
    )
    return VendorDetailResponse(
        vendor=VendorResponse.model_validate(vendor),
        banking=[BankingResponse.model_validate(b) for b in banking],
        documents=[DocumentResponse.model_validate(d) for d in documents],
        contracts=[ContractResponse.model_validate(c) for c in contracts],
    )


@router.patch("/{vendor_id}", response_model=VendorResponse)
async def update_vendor_endpoint(
    request: Request,
    vendor_id: str,
    body: VendorUpdateRequest,
    user: AuthUser = Depends(require_permission("vendor", "master", action="update")),
):
    await _require_vendor(request, vendor_id)
    try:
        row = await vendor_service.update_vendor(
            request.app.state.db_pool,
            vendor_id,
            body.model_dump(exclude_unset=True),
            actor_user_id=str(user.user_id),
        )
    except vendor_service.NotNullPatchError as e:
        raise _patch_to_400(e) from e
    if not row:
        raise HTTPException(404, detail={"code": "vendor_not_found"})
    return VendorResponse.model_validate(row)


@router.delete("/{vendor_id}", status_code=204)
async def delete_vendor_endpoint(
    request: Request,
    vendor_id: str,
    user: AuthUser = Depends(require_permission("vendor", "master", action="delete")),
):
    ok = await vendor_service.soft_delete_vendor(
        request.app.state.db_pool, vendor_id, actor_user_id=str(user.user_id),
    )
    if not ok:
        raise HTTPException(404, detail={"code": "vendor_not_found_or_already_deleted"})


@router.post("/{vendor_id}/approve", response_model=VendorResponse)
async def approve_vendor_endpoint(
    request: Request,
    vendor_id: str,
    user: AuthUser = Depends(require_permission("vendor", "master", action="approve")),
):
    """SCM Head / admin sign-off. Sets approved_by + approved_at.

    Gated by a separate `approve` action so this can't be reached via the
    generic update permission. Pre-conditions (KYC + primary banking) are
    enforced inside vendor_service.approve_vendor() — surfaced as 409 with
    a structured `code`.
    """
    try:
        row = await vendor_service.approve_vendor(
            request.app.state.db_pool, vendor_id, approver_user_id=str(user.user_id),
        )
    except vendor_service.ApprovalError as e:
        raise HTTPException(
            409, detail={"code": e.code, "msg": str(e)},
        ) from e
    if not row:
        raise HTTPException(404, detail={"code": "vendor_not_found"})
    return VendorResponse.model_validate(row)


# ── combined create: vendor + documents in one multipart request ─────────


@router.post(
    "/with-documents",
    response_model=VendorWithDocumentsResponse,
    status_code=201,
)
async def create_vendor_with_documents(
    request: Request,
    vendor: str = Form(
        ...,
        description="VendorCreateRequest body as a JSON string (the same shape as POST /api/v1/vendors body).",
    ),
    docs_meta: str = Form(
        "[]",
        description=(
            "JSON array of per-file metadata, one entry per uploaded file in "
            "the same order. Each entry: {\"doc_type\": <enum>, \"doc_number\": "
            "<optional>}. Pass \"[]\" or omit if no files."
        ),
    ),
    files: list[UploadFile] = File(
        default_factory=list,
        description="Uploaded compliance documents (PDF / JPEG / PNG).",
    ),
    user: AuthUser = Depends(require_permission("vendor", "master", action="create")),
):
    """One-shot vendor onboarding: create the vendor row, then push each
    uploaded document to S3, extract its fields via the Claude API, and
    save a `vendor_document` row.

    Atomicity: the vendor is committed first. Each document upload is then
    attempted independently — partial failures are returned in the
    `failures` array so the caller can retry just those files. The vendor
    row remains intact regardless of per-document outcomes.

    Permissions: the caller needs `vendor.master.create`. The same role
    is implicitly trusted to seed initial compliance documents — saves a
    second permission check for the onboarding flow.
    """
    pool = request.app.state.db_pool
    settings = request.app.state.settings
    actor_user_id = str(user.user_id)

    # ── parse the JSON form fields ──────────────────────────────────────
    try:
        vendor_data = json.loads(vendor)
    except json.JSONDecodeError as e:
        raise HTTPException(
            400,
            detail={"code": "invalid_vendor_json", "msg": str(e)},
        ) from e
    try:
        vendor_body = VendorCreateRequest.model_validate(vendor_data)
    except ValidationError as e:
        raise HTTPException(
            422,
            detail={"code": "vendor_validation_failed", "errors": e.errors()},
        ) from e

    try:
        meta_list = json.loads(docs_meta) if docs_meta else []
    except json.JSONDecodeError as e:
        raise HTTPException(
            400,
            detail={"code": "invalid_docs_meta_json", "msg": str(e)},
        ) from e
    if not isinstance(meta_list, list):
        raise HTTPException(
            400,
            detail={"code": "docs_meta_must_be_array"},
        )
    if len(meta_list) != len(files):
        raise HTTPException(
            400,
            detail={
                "code": "docs_meta_length_mismatch",
                "files_count": len(files),
                "meta_count": len(meta_list),
            },
        )
    if len(files) > _MAX_DOCS_PER_REQUEST:
        # Bound wall-clock + memory; retry leftover files via /upload-and-save.
        raise HTTPException(
            400,
            detail={
                "code": "too_many_files",
                "limit": _MAX_DOCS_PER_REQUEST,
                "actual": len(files),
            },
        )

    # ── H1: second permission gate — `vendor.master.create` alone does
    # not implicitly grant `vendor.document.create`. Skip the check
    # when no files were uploaded (the endpoint degrades to "vendor only").
    if files:
        async with pool.acquire() as conn:
            doc_allowed = await check_permission(
                conn, user.role_id, user.is_admin,
                "vendor", "document", action="create",
                entity=user.entity or None,
            )
        if not doc_allowed:
            raise HTTPException(
                403,
                detail={
                    "code": "forbidden",
                    "msg": "vendor.document.create permission required when uploading documents",
                    "module": "vendor", "sub_module": "document", "action": "create",
                },
            )

    # ── Read + validate every file up-front (before any DB write or S3
    # PUT). This way a single malformed file rejects the whole batch
    # with a 4xx instead of leaving an orphan vendor with partial docs.
    prepared: list[tuple[dict, bytes, str, str | None]] = []
    for idx, (file, meta) in enumerate(zip(files, meta_list, strict=True)):
        if not isinstance(meta, dict) or not meta.get("doc_type"):
            raise HTTPException(
                400,
                detail={
                    "code": "invalid_docs_meta_entry",
                    "index": idx,
                    "msg": "each docs_meta entry must be an object with a 'doc_type' field",
                },
            )
        content, mime, filename = await _sniff_and_read_upload(file)
        prepared.append((meta, content, mime, filename))

    # ── create the vendor first ─────────────────────────────────────────
    vendor_row = await vendor_service.create_vendor(
        pool,
        vendor_body.model_dump(exclude_unset=True),
        actor_user_id=actor_user_id,
    )
    supplier_code = vendor_row["supplier_code"]
    vendor_id = vendor_row["vendor_id"]

    # ── per-file upload + extract + save (sequential, best-effort) ──────
    # Sequential rather than parallel because running N docs concurrently
    # was tripping two ceilings:
    #   * Anthropic 429s — N parallel /messages calls hit the per-minute
    #     token+request rate limit even at modest N (~5+).
    #   * urllib3 S3 pool exhaustion — the boto3 default pool size is 10,
    #     so >10 in-flight S3 PUTs to the same bucket logged "Connection
    #     pool is full, discarding connection" and silently churned
    #     sockets.
    # A short sleep between docs gives the rate-limit window time to
    # refill and keeps the S3 pool drained. Inner s3+claude gather inside
    # document_service.upload_and_save is unaffected — that's only 2
    # ops at a time, well below either ceiling.
    INTER_DOC_PAUSE_S = 1.5

    async def _one(meta: dict, content: bytes, mime: str, filename: str | None):
        doc_type = meta["doc_type"]
        try:
            base_payload: dict | None = None
            if meta.get("doc_number"):
                base_payload = {"doc_number": meta["doc_number"]}
            row, extracted = await document_service.upload_and_save(
                pool,
                settings,
                vendor_id=vendor_id,
                supplier_code=supplier_code,
                doc_type=doc_type,
                file_bytes=content,
                mime_type=mime,
                original_filename=filename,
                actor_user_id=actor_user_id,
                base_payload=base_payload,
            )
            return ("ok", DocumentUploadResponse(
                document=DocumentResponse.model_validate(row),
                extracted=extracted,
            ))
        except RuntimeError as e:
            return ("err", DocumentUploadFailure(
                doc_type=doc_type, filename=filename, error=f"storage_unavailable: {e}",
            ))
        except Exception as e:  # noqa: BLE001
            logger.exception("vendor_with_documents.doc_failed vendor_id=%s", vendor_id)
            return ("err", DocumentUploadFailure(
                doc_type=doc_type, filename=filename, error=repr(e),
            ))

    results = []
    for idx, (meta, c, m, f) in enumerate(prepared):
        results.append(await _one(meta, c, m, f))
        # Sleep between docs but not after the last one (no point waiting
        # before the response goes out).
        if idx < len(prepared) - 1:
            await asyncio.sleep(INTER_DOC_PAUSE_S)

    documents: list[DocumentUploadResponse] = []
    failures: list[DocumentUploadFailure] = []
    for tag, payload in results:
        if tag == "ok":
            documents.append(payload)
        else:
            failures.append(payload)

    return VendorWithDocumentsResponse(
        vendor=VendorResponse.model_validate(vendor_row),
        documents=documents,
        failures=failures,
    )


# ── vendor_banking ───────────────────────────────────────────────────────


@router.post("/{vendor_id}/banking", response_model=BankingResponse, status_code=201)
async def create_banking_endpoint(
    request: Request,
    vendor_id: str,
    body: BankingCreateRequest,
    user: AuthUser = Depends(require_permission("vendor", "banking", action="create")),
):
    await _require_vendor(request, vendor_id)
    row = await banking_service.create_banking(
        request.app.state.db_pool, vendor_id, body.model_dump(exclude_unset=True),
        actor_user_id=str(user.user_id),
    )
    return BankingResponse.model_validate(row)


@router.get("/{vendor_id}/banking", response_model=list[BankingResponse])
async def list_banking_endpoint(
    request: Request,
    vendor_id: str,
    active_only: bool = Query(False),
    user: AuthUser = Depends(require_permission("vendor", "banking", action="view")),
):
    await _require_vendor(request, vendor_id)
    rows = await banking_service.list_banking(
        request.app.state.db_pool, vendor_id, active_only=active_only,
    )
    return [BankingResponse.model_validate(r) for r in rows]


@router.patch("/{vendor_id}/banking/{bank_id}", response_model=BankingResponse)
async def update_banking_endpoint(
    request: Request,
    vendor_id: str,
    bank_id: str,
    body: BankingUpdateRequest,
    user: AuthUser = Depends(require_permission("vendor", "banking", action="update")),
):
    await _require_vendor(request, vendor_id)
    # Scope the UPDATE to (bank_id, vendor_id) so an attacker who knows a
    # bank_id of vendor B can't mutate it by passing vendor_id=A in the URL.
    try:
        row = await banking_service.update_banking(
            request.app.state.db_pool,
            bank_id,
            body.model_dump(exclude_unset=True),
            vendor_id=vendor_id,
            actor_user_id=str(user.user_id),
        )
    except vendor_service.NotNullPatchError as e:
        raise _patch_to_400(e) from e
    if not row:
        raise HTTPException(404, detail={"code": "banking_not_found"})
    return BankingResponse.model_validate(row)


@router.delete("/{vendor_id}/banking/{bank_id}", status_code=204)
async def delete_banking_endpoint(
    request: Request,
    vendor_id: str,
    bank_id: str,
    user: AuthUser = Depends(require_permission("vendor", "banking", action="delete")),
):
    pool = request.app.state.db_pool
    cur = await banking_service.get_banking(pool, bank_id)
    if not cur or cur.get("vendor_id") != vendor_id:
        raise HTTPException(404, detail={"code": "banking_not_found"})
    await banking_service.delete_banking(pool, bank_id, actor_user_id=str(user.user_id))


@router.post(
    "/{vendor_id}/banking/{bank_id}/set-primary",
    response_model=BankingResponse,
)
async def set_primary_banking(
    request: Request,
    vendor_id: str,
    bank_id: str,
    user: AuthUser = Depends(require_permission("vendor", "banking", action="update")),
):
    # L2: banking_service.set_primary() already enforces (bank_id, vendor_id)
    # ownership inside the txn — the redundant _require_vendor() check has
    # been removed.
    row = await banking_service.set_primary(
        request.app.state.db_pool, vendor_id, bank_id,
        actor_user_id=str(user.user_id),
    )
    if not row:
        raise HTTPException(404, detail={"code": "banking_not_found_for_vendor"})
    return BankingResponse.model_validate(row)


# ── vendor_document ──────────────────────────────────────────────────────


@router.post("/{vendor_id}/documents", response_model=DocumentResponse, status_code=201)
async def create_document_endpoint(
    request: Request,
    vendor_id: str,
    body: DocumentManualCreate,
    user: AuthUser = Depends(require_permission("vendor", "document", action="create")),
):
    """Manual document entry — client supplies metadata + optional CSV
    of pre-uploaded s3_urls. No Claude call here.
    """
    await _require_vendor(request, vendor_id)
    row = await document_service.create_document(
        request.app.state.db_pool,
        vendor_id,
        body.model_dump(exclude_unset=True),
        actor_user_id=str(user.user_id),
    )
    return DocumentResponse.model_validate(row)


@router.get("/{vendor_id}/documents", response_model=list[DocumentResponse])
async def list_documents_endpoint(
    request: Request,
    vendor_id: str,
    doc_type: str | None = Query(None),
    expiring_within_days: int | None = Query(None, ge=0, le=365),
    user: AuthUser = Depends(require_permission("vendor", "document", action="view")),
):
    await _require_vendor(request, vendor_id)
    rows = await document_service.list_documents(
        request.app.state.db_pool,
        vendor_id,
        doc_type=doc_type,
        expiring_within_days=expiring_within_days,
    )
    return [DocumentResponse.model_validate(r) for r in rows]


@router.patch("/{vendor_id}/documents/{doc_id}", response_model=DocumentResponse)
async def update_document_endpoint(
    request: Request,
    vendor_id: str,
    doc_id: str,
    body: DocumentUpdate,
    user: AuthUser = Depends(require_permission("vendor", "document", action="update")),
):
    pool = request.app.state.db_pool
    cur = await document_service.get_document(pool, doc_id)
    if not cur or cur.get("vendor_id") != vendor_id:
        raise HTTPException(404, detail={"code": "document_not_found"})
    try:
        row = await document_service.update_document(
            pool, doc_id, body.model_dump(exclude_unset=True),
            actor_user_id=str(user.user_id),
        )
    except vendor_service.NotNullPatchError as e:
        raise _patch_to_400(e) from e
    return DocumentResponse.model_validate(row)


@router.delete("/{vendor_id}/documents/{doc_id}", status_code=204)
async def delete_document_endpoint(
    request: Request,
    vendor_id: str,
    doc_id: str,
    user: AuthUser = Depends(require_permission("vendor", "document", action="delete")),
):
    pool = request.app.state.db_pool
    cur = await document_service.get_document(pool, doc_id)
    if not cur or cur.get("vendor_id") != vendor_id:
        raise HTTPException(404, detail={"code": "document_not_found"})
    await document_service.delete_document(pool, doc_id, actor_user_id=str(user.user_id))


@router.post("/{vendor_id}/documents/extract", response_model=DocExtractResponse)
async def extract_document_dry_run(
    request: Request,
    vendor_id: str,
    doc_type: str = Form(...),
    file: UploadFile = File(...),
    user: AuthUser = Depends(require_permission("vendor", "document", action="create")),
):
    """Upload file to S3 + run Claude extraction; DOES NOT save a row.

    Caller reviews / edits the returned fields, then POSTs to
    /documents to persist (passing s3_url back in the s3_urls CSV).
    """
    vendor = await _require_vendor(request, vendor_id)
    content, mime, filename = await _sniff_and_read_upload(file)
    try:
        s3_url, extracted = await document_service.extract_only(
            request.app.state.settings,
            supplier_code=vendor["supplier_code"],
            doc_type=doc_type,
            file_bytes=content,
            mime_type=mime,
            original_filename=filename,
        )
    except RuntimeError as e:
        # Storage not configured.
        raise HTTPException(503, detail={"code": "storage_unavailable", "msg": str(e)}) from e
    return DocExtractResponse(s3_url=s3_url, doc_type=doc_type, extracted=extracted)


@router.post(
    "/{vendor_id}/documents/upload-and-save",
    response_model=DocumentUploadResponse,
    status_code=201,
)
async def upload_and_save_document(
    request: Request,
    vendor_id: str,
    doc_type: str = Form(...),
    doc_number: str | None = Form(None),
    file: UploadFile = File(...),
    user: AuthUser = Depends(require_permission("vendor", "document", action="create")),
):
    """One-shot: S3 upload + Claude extraction + insert row.

    Client supplies an OPTIONAL `doc_number` to override or skip the
    Claude-extracted value. Other date/status fields come from Claude
    only; the caller can PATCH afterwards to refine them.
    """
    vendor = await _require_vendor(request, vendor_id)
    content, mime, filename = await _sniff_and_read_upload(file)
    try:
        row, extracted = await document_service.upload_and_save(
            request.app.state.db_pool,
            request.app.state.settings,
            vendor_id=vendor_id,
            supplier_code=vendor["supplier_code"],
            doc_type=doc_type,
            file_bytes=content,
            mime_type=mime,
            original_filename=filename,
            actor_user_id=str(user.user_id),
            base_payload={"doc_number": doc_number} if doc_number else None,
        )
    except RuntimeError as e:
        raise HTTPException(503, detail={"code": "storage_unavailable", "msg": str(e)}) from e
    return DocumentUploadResponse(
        document=DocumentResponse.model_validate(row),
        extracted=extracted,
    )


@router.post(
    "/{vendor_id}/documents/{doc_id}/append-file",
    response_model=DocumentResponse,
)
async def append_document_file(
    request: Request,
    vendor_id: str,
    doc_id: str,
    file: UploadFile = File(...),
    user: AuthUser = Depends(require_permission("vendor", "document", action="update")),
):
    """Append a file to an existing document — pushes the new URL onto the
    comma-separated s3_urls. No re-extraction.
    """
    pool = request.app.state.db_pool
    vendor = await _require_vendor(request, vendor_id)
    cur = await document_service.get_document(pool, doc_id)
    if not cur or cur.get("vendor_id") != vendor_id:
        raise HTTPException(404, detail={"code": "document_not_found"})

    content, mime, filename = await _sniff_and_read_upload(file)
    try:
        row = await document_service.append_file(
            pool,
            request.app.state.settings,
            doc_id=doc_id,
            supplier_code=vendor["supplier_code"],
            doc_type=cur["doc_type"],
            file_bytes=content,
            mime_type=mime,
            original_filename=filename,
        )
    except RuntimeError as e:
        raise HTTPException(503, detail={"code": "storage_unavailable", "msg": str(e)}) from e
    if not row:
        raise HTTPException(404, detail={"code": "document_not_found"})
    return DocumentResponse.model_validate(row)


# ── vendor_contract ──────────────────────────────────────────────────────


@router.post("/{vendor_id}/contracts", response_model=ContractResponse, status_code=201)
async def create_contract_endpoint(
    request: Request,
    vendor_id: str,
    body: ContractManualCreate,
    user: AuthUser = Depends(require_permission("vendor", "contract", action="create")),
):
    await _require_vendor(request, vendor_id)
    row = await contract_service.create_contract(
        request.app.state.db_pool,
        vendor_id,
        body.model_dump(exclude_unset=True),
        actor_user_id=str(user.user_id),
    )
    return ContractResponse.model_validate(row)


@router.get("/{vendor_id}/contracts", response_model=list[ContractResponse])
async def list_contracts_endpoint(
    request: Request,
    vendor_id: str,
    contract_type: str | None = Query(None),
    user: AuthUser = Depends(require_permission("vendor", "contract", action="view")),
):
    await _require_vendor(request, vendor_id)
    rows = await contract_service.list_contracts(
        request.app.state.db_pool, vendor_id, contract_type=contract_type,
    )
    return [ContractResponse.model_validate(r) for r in rows]


@router.patch("/{vendor_id}/contracts/{contract_id}", response_model=ContractResponse)
async def update_contract_endpoint(
    request: Request,
    vendor_id: str,
    contract_id: str,
    body: ContractUpdate,
    user: AuthUser = Depends(require_permission("vendor", "contract", action="update")),
):
    pool = request.app.state.db_pool
    cur = await contract_service.get_contract(pool, contract_id)
    if not cur or cur.get("vendor_id") != vendor_id:
        raise HTTPException(404, detail={"code": "contract_not_found"})
    row = await contract_service.update_contract(
        pool, contract_id, body.model_dump(exclude_unset=True),
        actor_user_id=str(user.user_id),
    )
    return ContractResponse.model_validate(row)


@router.delete("/{vendor_id}/contracts/{contract_id}", status_code=204)
async def delete_contract_endpoint(
    request: Request,
    vendor_id: str,
    contract_id: str,
    user: AuthUser = Depends(require_permission("vendor", "contract", action="delete")),
):
    pool = request.app.state.db_pool
    cur = await contract_service.get_contract(pool, contract_id)
    if not cur or cur.get("vendor_id") != vendor_id:
        raise HTTPException(404, detail={"code": "contract_not_found"})
    await contract_service.delete_contract(pool, contract_id, actor_user_id=str(user.user_id))


@router.post("/{vendor_id}/contracts/extract", response_model=ContractExtractResponse)
async def extract_contract_dry_run(
    request: Request,
    vendor_id: str,
    file: UploadFile = File(...),
    user: AuthUser = Depends(require_permission("vendor", "contract", action="create")),
):
    vendor = await _require_vendor(request, vendor_id)
    content, mime, filename = await _sniff_and_read_upload(file)
    try:
        s3_url, extracted = await contract_service.extract_only(
            request.app.state.settings,
            supplier_code=vendor["supplier_code"],
            file_bytes=content,
            mime_type=mime,
            original_filename=filename,
        )
    except RuntimeError as e:
        raise HTTPException(503, detail={"code": "storage_unavailable", "msg": str(e)}) from e
    return ContractExtractResponse(s3_url=s3_url, extracted=extracted)


@router.post(
    "/{vendor_id}/contracts/upload-and-save",
    response_model=ContractUploadResponse,
    status_code=201,
)
async def upload_and_save_contract(
    request: Request,
    vendor_id: str,
    file: UploadFile = File(...),
    contract_type: str | None = Form(None),
    user: AuthUser = Depends(require_permission("vendor", "contract", action="create")),
):
    vendor = await _require_vendor(request, vendor_id)
    content, mime, filename = await _sniff_and_read_upload(file)
    base = {"contract_type": contract_type} if contract_type else None
    try:
        row, extracted = await contract_service.upload_and_save(
            request.app.state.db_pool,
            request.app.state.settings,
            vendor_id=vendor_id,
            supplier_code=vendor["supplier_code"],
            file_bytes=content,
            mime_type=mime,
            original_filename=filename,
            actor_user_id=str(user.user_id),
            base_payload=base,
        )
    except RuntimeError as e:
        raise HTTPException(503, detail={"code": "storage_unavailable", "msg": str(e)}) from e
    return ContractUploadResponse(
        contract=ContractResponse.model_validate(row),
        extracted=extracted,
    )


@router.post(
    "/{vendor_id}/contracts/{contract_id}/append-file",
    response_model=ContractResponse,
)
async def append_contract_file(
    request: Request,
    vendor_id: str,
    contract_id: str,
    file: UploadFile = File(...),
    user: AuthUser = Depends(require_permission("vendor", "contract", action="update")),
):
    pool = request.app.state.db_pool
    vendor = await _require_vendor(request, vendor_id)
    cur = await contract_service.get_contract(pool, contract_id)
    if not cur or cur.get("vendor_id") != vendor_id:
        raise HTTPException(404, detail={"code": "contract_not_found"})

    content, mime, filename = await _sniff_and_read_upload(file)
    try:
        row = await contract_service.append_file(
            pool,
            request.app.state.settings,
            contract_id=contract_id,
            supplier_code=vendor["supplier_code"],
            file_bytes=content,
            mime_type=mime,
            original_filename=filename,
        )
    except RuntimeError as e:
        raise HTTPException(503, detail={"code": "storage_unavailable", "msg": str(e)}) from e
    if not row:
        raise HTTPException(404, detail={"code": "contract_not_found"})
    return ContractResponse.model_validate(row)


# ── extract-bulk + submit-staged (new onboarding flow) ───────────────────


@router.post("/extract-bulk", response_model=ExtractBulkResponse)
async def extract_bulk_endpoint(
    request: Request,
    docs_meta: str = Form(
        "[]",
        description=(
            "Optional JSON array of per-file `{doc_type}` hints, parallel to "
            "files[]. Pass [] or omit to let Claude infer the type from "
            "content."
        ),
    ),
    files: list[UploadFile] = File(default_factory=list),
    user: AuthUser = Depends(require_permission("vendor", "master", action="create")),
):
    """Phase 1 of the new onboarding flow. Uploads files BEFORE a vendor
    row exists and runs Claude extraction across all of them, returning a
    consolidated suggestion payload + `staging_id` the caller can hand
    back via /submit-staged.

    No vendor / banking / document / contract rows are written here.
    """
    if not files:
        raise HTTPException(400, detail={"code": "no_files"})
    if len(files) > _MAX_DOCS_PER_REQUEST:
        raise HTTPException(
            400,
            detail={
                "code": "too_many_files",
                "limit": _MAX_DOCS_PER_REQUEST,
                "actual": len(files),
            },
        )

    try:
        meta_list = json.loads(docs_meta) if docs_meta else []
    except json.JSONDecodeError as e:
        raise HTTPException(
            400, detail={"code": "invalid_docs_meta_json", "msg": str(e)},
        ) from e
    if not isinstance(meta_list, list):
        raise HTTPException(400, detail={"code": "docs_meta_must_be_array"})
    if meta_list and len(meta_list) != len(files):
        raise HTTPException(
            400,
            detail={
                "code": "docs_meta_length_mismatch",
                "files_count": len(files),
                "meta_count": len(meta_list),
            },
        )

    # Read + sniff every upload up-front so a single bad file fails the
    # whole batch with a 4xx (rather than partially-staging in S3).
    prepared: list[tuple[bytes, str, str | None, str | None]] = []
    for idx, file in enumerate(files):
        meta = meta_list[idx] if idx < len(meta_list) else {}
        declared_type = (meta or {}).get("doc_type") if isinstance(meta, dict) else None
        content, mime, filename = await _sniff_and_read_upload(file)
        prepared.append((content, mime, filename, declared_type))

    try:
        result = await extraction_service.extract_bulk(
            request.app.state.db_pool,
            request.app.state.settings,
            files=prepared,
            actor_user_id=str(user.user_id),
        )
    except RuntimeError as e:
        raise HTTPException(503, detail={"code": "storage_unavailable", "msg": str(e)}) from e
    except extraction_service.StagingError as e:
        # `migration_not_applied` is operator-actionable (run 030_vendor_history.sql).
        # Surface as 503 so the UI can prompt the admin to apply migrations.
        raise HTTPException(503, detail={"code": e.code, "msg": str(e)}) from e
    return ExtractBulkResponse.model_validate(result)


@router.post("/submit-staged", response_model=SubmitStagedResponse, status_code=201)
async def submit_staged_endpoint(
    request: Request,
    body: SubmitStagedRequest,
    user: AuthUser = Depends(require_permission("vendor", "master", action="create")),
):
    """Phase 2 of the new onboarding flow. Commits the (operator-edited)
    payload across vendor_master + banking + document + contract in one
    transaction and marks the staging row consumed.
    """
    try:
        result = await extraction_service.submit_staged(
            request.app.state.db_pool,
            request.app.state.settings,
            staging_id=body.staging_id,
            vendor=body.vendor.model_dump(exclude_unset=True),
            banking=[b.model_dump(exclude_unset=True) for b in body.banking],
            documents=[d.model_dump(exclude_unset=True) for d in body.documents],
            contracts=[c.model_dump(exclude_unset=True) for c in body.contracts],
            actor_user_id=str(user.user_id),
        )
    except extraction_service.StagingError as e:
        # 409 — caller's staging state precludes submit; they should re-extract.
        raise HTTPException(409, detail={"code": e.code, "msg": str(e)}) from e
    return SubmitStagedResponse.model_validate(result)


# ── history endpoints (state management) ─────────────────────────────────


@router.get("/{vendor_id}/history", response_model=HistoryListResponse)
async def list_vendor_history_endpoint(
    request: Request,
    vendor_id: str,
    operation: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user: AuthUser = Depends(require_permission("vendor", "master", action="view_history")),
):
    """Master timeline. Each entry carries `previous_state` + `new_state` +
    a field-level `diff` so the UI can render a "from / to" view per row.
    """
    await _require_vendor(request, vendor_id)
    entries, total = await history_service.list_history(
        request.app.state.db_pool, "vendor",
        vendor_id=vendor_id, operation=operation,
        page=page, page_size=page_size,
    )
    return HistoryListResponse(
        entries=[HistoryEntry.model_validate(e) for e in entries],
        total=total, page=page, page_size=page_size,
    )


@router.get("/{vendor_id}/history/{history_id}", response_model=HistoryEntry)
async def get_vendor_history_entry(
    request: Request,
    vendor_id: str,
    history_id: int,
    user: AuthUser = Depends(require_permission("vendor", "master", action="view_history")),
):
    entry = await history_service.get_history_entry(
        request.app.state.db_pool, "vendor", history_id, vendor_id=vendor_id,
    )
    if entry is None:
        raise HTTPException(404, detail={"code": "history_not_found"})
    return HistoryEntry.model_validate(entry)


@router.post("/{vendor_id}/history/{history_id}/revert", response_model=VendorResponse)
async def revert_vendor_history(
    request: Request,
    vendor_id: str,
    history_id: int,
    body: HistoryRevertRequest,
    user: AuthUser = Depends(require_permission("vendor", "master", action="revert")),
):
    """Apply the `previous_state` of an UPDATE / APPROVE snapshot as a
    fresh PATCH. The revert is itself recorded in history (operation =
    'revert'); the original entry is left untouched (append-only).
    """
    entry = await history_service.get_history_entry(
        request.app.state.db_pool, "vendor", history_id, vendor_id=vendor_id,
    )
    if entry is None:
        raise HTTPException(404, detail={"code": "history_not_found"})
    try:
        patch = history_service.build_revert_patch(entry)
    except ValueError as e:
        raise HTTPException(400, detail={"code": "not_revertable", "msg": str(e)}) from e
    if not patch:
        raise HTTPException(
            400,
            detail={
                "code": "nothing_to_revert",
                "msg": "this entry's diff is empty or only covers protected fields",
            },
        )
    effective_reason = body.reason or f"revert of history {history_id}"
    try:
        row = await vendor_service.update_vendor(
            request.app.state.db_pool, vendor_id, patch,
            actor_user_id=str(user.user_id),
            source="revert", reason=effective_reason,
        )
    except vendor_service.NotNullPatchError as e:
        raise _patch_to_400(e) from e
    if row is None:
        raise HTTPException(404, detail={"code": "vendor_not_found"})
    return VendorResponse.model_validate(row)


@router.get(
    "/{vendor_id}/banking/{bank_id}/history",
    response_model=HistoryListResponse,
)
async def list_banking_history_endpoint(
    request: Request,
    vendor_id: str,
    bank_id: str,
    user: AuthUser = Depends(require_permission("vendor", "master", action="view_history")),
):
    await _require_vendor(request, vendor_id)
    entries, total = await history_service.list_history(
        request.app.state.db_pool, "banking",
        vendor_id=vendor_id, parent_id=bank_id,
    )
    return HistoryListResponse(
        entries=[HistoryEntry.model_validate(e) for e in entries],
        total=total, page=1, page_size=len(entries),
    )


@router.get(
    "/{vendor_id}/documents/{doc_id}/history",
    response_model=HistoryListResponse,
)
async def list_document_history_endpoint(
    request: Request,
    vendor_id: str,
    doc_id: str,
    user: AuthUser = Depends(require_permission("vendor", "master", action="view_history")),
):
    await _require_vendor(request, vendor_id)
    entries, total = await history_service.list_history(
        request.app.state.db_pool, "document",
        vendor_id=vendor_id, parent_id=doc_id,
    )
    return HistoryListResponse(
        entries=[HistoryEntry.model_validate(e) for e in entries],
        total=total, page=1, page_size=len(entries),
    )


@router.get(
    "/{vendor_id}/contracts/{contract_id}/history",
    response_model=HistoryListResponse,
)
async def list_contract_history_endpoint(
    request: Request,
    vendor_id: str,
    contract_id: str,
    user: AuthUser = Depends(require_permission("vendor", "master", action="view_history")),
):
    await _require_vendor(request, vendor_id)
    entries, total = await history_service.list_history(
        request.app.state.db_pool, "contract",
        vendor_id=vendor_id, parent_id=contract_id,
    )
    return HistoryListResponse(
        entries=[HistoryEntry.model_validate(e) for e in entries],
        total=total, page=1, page_size=len(entries),
    )
