import asyncpg

from app.config import Settings


async def create_pool(settings: Settings) -> asyncpg.Pool:
    return await asyncpg.create_pool(settings.DATABASE_URL, min_size=0, max_size=3)


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()
