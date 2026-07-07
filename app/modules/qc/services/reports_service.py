"""QC Inward Inspection — report generation stubs.

Functional stubs only — no S3 integration yet.  These functions verify the
inspection state and return a placeholder report response.

  generate_rm_report  — RM (raw material approval) report; requires passed verdict
  generate_ncr_report — NCR report; requires failed verdict + ncr_no assigned
"""

from __future__ import annotations

from app.core.middleware.request_context import AuthError


async def generate_rm_report(pool, inspection_id: int) -> dict:
    """Return a placeholder RM report descriptor.

    Raises 409 if the inspection has not been approved yet.

    # TODO: real RM report generation + S3 upload when document pipeline is ready.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT verdict FROM qc_inward_inspection WHERE inspection_id = $1",
            inspection_id,
        )
    if row is None:
        raise AuthError(
            "inspection_not_found",
            f"Inspection {inspection_id} not found.",
            404,
        )
    if row["verdict"] != "passed":
        raise AuthError(
            "not_approved",
            "Inspection isn't approved yet.  Verdict must be 'passed' to generate the RM report.",
            409,
        )
    return {
        "report_id": f"RM-{inspection_id}",
        "download_url": None,
    }


async def generate_ncr_report(pool, inspection_id: int) -> dict:
    """Return a placeholder NCR report descriptor.

    Raises 409 if the inspection isn't rejected, or if no ncr_no has been assigned.

    # TODO: real NCR report generation + S3 upload when document pipeline is ready.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT verdict, ncr_no FROM qc_inward_inspection WHERE inspection_id = $1",
            inspection_id,
        )
    if row is None:
        raise AuthError(
            "inspection_not_found",
            f"Inspection {inspection_id} not found.",
            404,
        )
    if row["verdict"] != "failed":
        raise AuthError(
            "not_rejected",
            "Inspection isn't rejected.  Verdict must be 'failed' to generate the NCR report.",
            409,
        )
    if not row["ncr_no"]:
        raise AuthError(
            "ncr_not_assigned",
            "No NCR number has been assigned to this inspection yet.",
            409,
        )
    return {
        "report_id": f"NCR-{inspection_id}",
        "download_url": None,
    }
