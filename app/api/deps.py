"""Shared FastAPI dependencies."""

from collections.abc import AsyncIterator, Awaitable, Callable

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.kafka_producer import check_kafka
from app.clients.redis_client import check_redis, get_redis
from app.core.clock import Clock, system_clock
from app.db.postgres import check_database
from app.db.session import get_internal_session_factory, get_read_session_factory, get_session_factory

Probe = Callable[[], Awaitable[bool]]


def get_probes() -> dict[str, Probe]:
    """Readiness probes by dependency name. Tests override this dependency with fakes."""
    return {"postgres": check_database, "redis": check_redis, "kafka": check_kafka}


# --- Request-scoped collaborators (tests override these with a throwaway database, fakeredis and a FixedClock) ---


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """A read session for request plumbing (identity and membership lookups). It never commits: business writes
    use `unit_of_work()` in their own use case."""
    async with get_read_session_factory()() as session:
        yield session
        await session.rollback()


async def get_internal_session() -> AsyncIterator[AsyncSession]:
    """The session of the /internal routes (billing's hot path): one autocommit statement per query, no transaction."""
    async with get_internal_session_factory()() as session:
        yield session


def get_redis_client() -> Redis:
    return get_redis()


def get_clock() -> Clock:
    return system_clock


def get_session_factory_dep():
    """The session factory write routes open their unit of work on (tests override it with a throwaway database)."""
    return get_session_factory()
