import asyncpg
from .config import PAAS_DB

_pool: asyncpg.Pool | None = None

async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(**PAAS_DB, min_size=1, max_size=5)

async def close_pool() -> None:
    if _pool:
        await _pool.close()

def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("pool not initialised")
    return _pool
