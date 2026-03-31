import asyncio
import logging
import os
import sys as _sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.applications import Starlette
from starlette.routing import Mount

from app.config import Settings
from app.db.connection import create_pool, close_pool
from app.modules.so.router import router as so_router
from app.modules.purchase.router import router as purchase_router
from app.modules.production.router import router as production_router
from app.modules.so.services.item_matcher import load_master_items
from app.modules.production.services.master_ingest import run_master_ingest

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# Ensure repo root is in sys.path
_repo_root = str(Path(__file__).parent.parent)
if _repo_root not in _sys.path:
    _sys.path.insert(0, _repo_root)

from mcp_server import mcp as _mcp_instance  # noqa: E402
from mcp_viewer_server import mcp_viewer as _mcp_viewer_instance  # noqa: E402


# ---------------------------------------------------------------------------
# FastAPI app (REST API only — no MCP mount)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    settings = Settings()
    fastapi_app.state.settings = settings

    pool = await create_pool(settings)
    fastapi_app.state.db_pool = pool

    db_dir = Path(__file__).parent / "db"
    async with pool.acquire() as conn:
        await conn.execute((db_dir / "schema.sql").read_text())
        await conn.execute((db_dir / "migrate.sql").read_text())
        await conn.execute((db_dir / "po_schema.sql").read_text())
        await conn.execute((db_dir / "po_migrate.sql").read_text())
        await conn.execute((db_dir / "production_schema.sql").read_text())
        await conn.execute((db_dir / "production_migrate.sql").read_text())
    logger.info("Database schema ensured")

    master_items = await load_master_items(pool)
    fastapi_app.state.master_items = master_items

    data_dir = Path(__file__).parent / "data"
    if not data_dir.exists():
        data_dir = Path(__file__).parent.parent / "data"
    await run_master_ingest(pool, data_dir, master_items)

    # Keep-alive poller
    keep_alive_task = None
    backend_url = os.environ.get("RENDER_BACKEND_URL")

    async def _keep_alive():
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                await asyncio.sleep(420)
                try:
                    resp = await client.get(backend_url.rstrip("/") + "/api/v1/production/health")
                    logger.debug("Keep-alive → %s", resp.status_code)
                except Exception as e:
                    logger.warning("Keep-alive failed: %s", e)

    if backend_url:
        keep_alive_task = asyncio.create_task(_keep_alive())
        logger.info("Keep-alive poller started")

    yield

    if keep_alive_task:
        keep_alive_task.cancel()
    await close_pool(pool)
    logger.info("Shutdown complete")


fastapi_app = FastAPI(title="Candor Foods — Consumption Backend", version="0.3.0", lifespan=lifespan)

fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

fastapi_app.include_router(so_router)
fastapi_app.include_router(purchase_router)
fastapi_app.include_router(production_router)


@fastapi_app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Combined ASGI app — MCP at root, FastAPI at /api, viewer at /viewer
# ---------------------------------------------------------------------------
# Claude Desktop sends POST / → hits MCP directly (no subpath needed)
# REST API lives at /api/v1/... (prefixed by routers, works as-is)
# Viewer MCP at /viewer/

app = Starlette(
    routes=[
        Mount("/api", app=fastapi_app),
        Mount("/health", app=fastapi_app),
        Mount("/viewer", app=_mcp_viewer_instance.streamable_http_app()),
        Mount("/", app=_mcp_instance.streamable_http_app()),
    ],
)
