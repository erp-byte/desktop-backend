import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI

from app.config import Settings
from app.db.connection import create_pool, close_pool
from app.modules.so.router import router as so_router
from app.modules.purchase.router import router as purchase_router
from app.modules.production.router import router as production_router
from app.modules.so.services.item_matcher import load_master_items
from app.modules.production.services.master_ingest import run_master_ingest

logger = logging.getLogger(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    app.state.settings = settings

    # Database pool
    pool = await create_pool(settings)
    app.state.db_pool = pool

    # Run schema creation + migration
    db_dir = Path(__file__).parent / "db"
    async with pool.acquire() as conn:
        await conn.execute((db_dir / "schema.sql").read_text())
        await conn.execute((db_dir / "migrate.sql").read_text())
        await conn.execute((db_dir / "po_schema.sql").read_text())
        await conn.execute((db_dir / "po_migrate.sql").read_text())
        # Production schema + migration
        await conn.execute((db_dir / "production_schema.sql").read_text())
        await conn.execute((db_dir / "production_migrate.sql").read_text())
    logger.info("Database schema ensured")

    # Load master items for matching
    master_items = await load_master_items(pool)
    app.state.master_items = master_items

    # Ingest production master data (BOM, machines) — idempotent
    # data/ can be at app/data/ or repo root data/
    data_dir = Path(__file__).parent / "data"
    if not data_dir.exists():
        data_dir = Path(__file__).parent.parent / "data"
    await run_master_ingest(pool, data_dir, master_items)

    # Keep-alive poller for Render free tier (pings every 7 min to prevent spin-down)
    keep_alive_task = None
    backend_url = os.environ.get("RENDER_BACKEND_URL")

    async def _keep_alive():
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                await asyncio.sleep(420)  # 7 minutes
                try:
                    resp = await client.get(backend_url.rstrip("/") + "/health")
                    logger.debug("Keep-alive ping → %s", resp.status_code)
                except Exception as e:
                    logger.warning("Keep-alive ping failed: %s", e)

    if backend_url:
        keep_alive_task = asyncio.create_task(_keep_alive())
        logger.info("Keep-alive poller started for: %s", backend_url)

    # Start both MCP session managers (initialises their task groups)
    async with _mcp_instance.session_manager.run():
        async with _mcp_viewer_instance.session_manager.run():
            yield

        if keep_alive_task:
            keep_alive_task.cancel()
        await close_pool(pool)
        logger.info("Shutdown complete")


app = FastAPI(title="Candor Foods — Consumption Backend", version="0.3.0", lifespan=lifespan)
app.include_router(so_router)
app.include_router(purchase_router)
app.include_router(production_router)

# Mount MCP servers alongside FastAPI on the same port
# Ensure repo root is in sys.path (mcp_server.py and mcp_viewer_server.py live there)
import sys as _sys
_repo_root = str(Path(__file__).parent.parent)
if _repo_root not in _sys.path:
    _sys.path.insert(0, _repo_root)

# Full access (77 tools) at /mcp/
from mcp_server import mcp as _mcp_instance  # noqa: E402
_mcp_starlette = _mcp_instance.streamable_http_app()
_mcp_starlette.router.lifespan_context = None  # lifespan managed above
app.mount("/mcp", _mcp_starlette)

# Viewer access (34 tools, read-only) at /mcp-viewer/
from mcp_viewer_server import mcp_viewer as _mcp_viewer_instance  # noqa: E402
_mcp_viewer_starlette = _mcp_viewer_instance.streamable_http_app()
_mcp_viewer_starlette.router.lifespan_context = None
app.mount("/mcp-viewer", _mcp_viewer_starlette)



@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
