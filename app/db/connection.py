import asyncpg

from app.config import Settings


async def create_pool(settings: Settings) -> asyncpg.Pool:
    # Sizing and timeout are configurable rather than literals — see the
    # "Database pool" block in app/config.py for why 10 was too small and
    # why an unbounded command timeout leaked pool capacity.
    return await asyncpg.create_pool(
        settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
        # asyncpg wants None (not 0) to mean "no timeout".
        command_timeout=settings.DB_COMMAND_TIMEOUT or None,
    )


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()
