import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings
from app.db.connection import create_pool, close_pool
from app.modules.auth.router import router as auth_router
from app.modules.so.router import router as so_router
from app.modules.purchase.router import router as purchase_router
from app.modules.production.router import router as production_router
from app.modules.so.services.item_matcher import load_master_items
from app.modules.production.services.master_ingest import run_master_ingest

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


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
        await conn.execute((db_dir / "auth_schema.sql").read_text())
    logger.info("Database schema ensured")

    master_items = await load_master_items(pool)
    fastapi_app.state.master_items = master_items

    data_dir = Path(__file__).parent / "data"
    if not data_dir.exists():
        data_dir = Path(__file__).parent.parent / "data"
    await run_master_ingest(pool, data_dir, master_items)

    # Keep-alive poller — pings all Render services every 7 min to prevent spin-down
    keep_alive_task = None
    keep_alive_urls = []
    for env_key in ["RENDER_BACKEND_URL", "RENDER_PLANNER_MCP_URL", "RENDER_TRACKER_MCP_URL"]:
        url = os.environ.get(env_key, "")
        if url:
            keep_alive_urls.append(url.rstrip("/"))
    # Hardcoded fallback if env vars not set
    if not keep_alive_urls:
        keep_alive_urls = [
            "https://desktop-backend-vhf0.onrender.com",
            "https://desktop-backend-nk6k.onrender.com",
            "https://desktop-backend-el31.onrender.com",
        ]

    async def _keep_alive():
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                await asyncio.sleep(420)  # 7 minutes
                for url in keep_alive_urls:
                    try:
                        resp = await client.get(url + "/health" if "/api" not in url else url)
                        logger.debug("Keep-alive %s → %s", url, resp.status_code)
                    except Exception as e:
                        logger.warning("Keep-alive failed %s: %s", url, e)

    if keep_alive_urls:
        keep_alive_task = asyncio.create_task(_keep_alive())
        logger.info("Keep-alive poller started for: %s", keep_alive_urls)

    yield

    if keep_alive_task:
        keep_alive_task.cancel()
    await close_pool(pool)
    logger.info("Shutdown complete")


app = FastAPI(title="Candor Foods — Consumption Backend", version="0.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(so_router)
app.include_router(purchase_router)
app.include_router(production_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
