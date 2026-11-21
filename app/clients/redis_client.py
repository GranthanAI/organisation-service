"""Redis client (lazy, one per process) and its readiness probe."""

from typing import Optional

import redis.asyncio as aioredis

from app.core.config import settings

_client: Optional[aioredis.Redis] = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=settings.READY_PROBE_TIMEOUT_SECONDS,
            socket_timeout=settings.READY_PROBE_TIMEOUT_SECONDS,
        )
    return _client


async def check_redis() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception:
        return False


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
