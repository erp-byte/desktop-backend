"""Inter-Unit Transfer module — replica port of the production /interunit/* API.

Ported from `transfer_backend_reference/services/ims_service` (SQLAlchemy) into
the server_replica stack (asyncpg + JWT auth), mounted at /api/v1/transfer/*.

Phase 1 scope: the Transfer Dashboard's read/detail/pending/delete surface.
Create/receive/print/analytics flows arrive in later phases.
"""
