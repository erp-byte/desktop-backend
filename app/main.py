import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import hmac as _hmac

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum
from pydantic import BaseModel

from app.config import Settings
from app.core.middleware import request_context
from app.db.connection import create_pool, close_pool
from app.modules.auth.router import router as auth_router
from app.modules.so.router import router as so_router
from app.modules.purchase.router import router as purchase_router
from app.modules.purchase.po_router import router as po_router
from app.modules.receipt.router import router as receipt_router
from app.modules.ncr.router import router as ncr_router
from app.modules.production.router import router as production_router
from app.modules.production.router_amendments import router as production_amendments_router
from app.modules.production.router_audit import router as production_audit_router
from app.modules.amendment_router import router as amendment_router
from app.modules.vendor.router import router as vendor_router
from app.modules.sample.router import router as sample_router
from app.modules.transfer.router import router as transfer_router
from app.modules.lookups_router import router as lookups_router
from app.modules.qc.router import router as qc_router
from app.modules.so.services.item_matcher import load_master_items
from app.modules.production.services.master_ingest import run_master_ingest

from app.webhooks.event_bus import event_bus, Event
from app.webhooks.dispatcher import dispatcher_loop
from app.webhooks.broadcaster import broadcaster_loop
from app.webhooks.router import router as webhook_router
from app.webhooks.ws_router import router as ws_router

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    settings = Settings()
    fastapi_app.state.settings = settings

    pool = await create_pool(settings)
    fastapi_app.state.db_pool = pool

    master_items = await load_master_items(pool)
    fastapi_app.state.master_items = master_items

    data_dir = Path(__file__).parent / "data"
    if not data_dir.exists():
        data_dir = Path(__file__).parent.parent / "data"
    await run_master_ingest(pool, data_dir, master_items)

    # Start webhook dispatcher and WebSocket broadcaster as background tasks
    bg_tasks = []
    bg_tasks.append(asyncio.create_task(dispatcher_loop(pool)))
    bg_tasks.append(asyncio.create_task(broadcaster_loop()))
    fastapi_app.state._webhook_tasks = bg_tasks

    yield

    # Cancel background tasks
    for t in bg_tasks:
        t.cancel()
    await asyncio.gather(*bg_tasks, return_exceptions=True)

    await close_pool(pool)
    logger.info("Shutdown complete")


app = FastAPI(title="Candor Foods — Consumption Backend", version="0.4.0", lifespan=lifespan)

# Middleware order matters: Starlette runs the LAST-added middleware OUTERMOST.
# We want RequestContext to wrap CORS so that:
#   • CORS preflight (OPTIONS) responses also carry X-Request-ID
#   • the structured error envelope wraps any failure CORS can produce
# Therefore: add CORS FIRST, then request_context.install LAST.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)
request_context.install(app)

app.include_router(auth_router)
app.include_router(so_router)
app.include_router(purchase_router)
app.include_router(po_router)
app.include_router(receipt_router)
app.include_router(ncr_router)
app.include_router(production_router)
app.include_router(production_amendments_router)
app.include_router(production_audit_router)
app.include_router(amendment_router)
app.include_router(vendor_router)
app.include_router(sample_router)
app.include_router(transfer_router)
app.include_router(lookups_router)
app.include_router(qc_router)
app.include_router(webhook_router)
app.include_router(ws_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Internal events endpoint (for MCP servers) ──

class InternalEventBody(BaseModel):
    event_type: str
    entity: str
    payload: dict
    actor: str = "mcp"
    target_roles: list[str] = []


@app.post("/internal/events")
async def receive_internal_event(body: InternalEventBody, request: Request):
    """MCP servers on remote Render instances POST here to inject events."""
    settings = request.app.state.settings
    token = settings.INTERNAL_WEBHOOK_TOKEN
    if not token:
        raise HTTPException(503, "Internal events not configured")

    auth = request.headers.get("Authorization", "")
    if not _hmac.compare_digest(auth, f"Bearer {token}"):
        raise HTTPException(401, "Invalid internal token")

    await event_bus.publish(Event(
        event_type=body.event_type,
        entity=body.entity,
        payload=body.payload,
        actor=body.actor,
        target_roles=body.target_roles,
    ))
    return {"accepted": True}


# AWS Lambda entry point
handler = Mangum(app, lifespan="on")
