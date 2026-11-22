"""Async sessions and the unit of work: ONE transaction per use case.

Repositories take the session and never commit. `unit_of_work()` commits when the block ends normally and rolls
everything back (membership, outbox and audit rows alike) on any exception. A constraint violation that
`app/db/errors.py` knows becomes its domain error (for example AlreadyInOrg).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import logger
from app.db.errors import map_integrity_error
from app.db.postgres import get_engine

_session_factory: async_sessionmaker[AsyncSession] | None = None
_read_factory: async_sessionmaker[AsyncSession] | None = None
_internal_factory: async_sessionmaker[AsyncSession] | None = None


class ResilientSession(AsyncSession):
    """A READ session that retries once when the pooled connection it got turns out to be dead (the database restarted, a
    proxy dropped it): there is no ping on every checkout any more. Never use it for writes: a retry would repeat them."""

    async def execute(self, *args, **kwargs):
        try:
            return await super().execute(*args, **kwargs)
        except DBAPIError as exc:
            if not exc.connection_invalidated:
                raise
            logger.warning("database connection was dropped, retrying the read once")
            await self.rollback()
            return await super().execute(*args, **kwargs)

    async def get(self, *args, **kwargs):
        try:
            return await super().get(*args, **kwargs)
        except DBAPIError as exc:
            if not exc.connection_invalidated:
                raise
            logger.warning("database connection was dropped, retrying the read once")
            await self.rollback()
            return await super().get(*args, **kwargs)


def get_read_session_factory() -> async_sessionmaker[AsyncSession]:
    """Read sessions for request plumbing (get_db_session): a transaction, retried once on a dropped connection."""
    global _read_factory
    if _read_factory is None:
        _read_factory = async_sessionmaker(bind=get_engine(), class_=ResilientSession, expire_on_commit=False, autoflush=False)
    return _read_factory


def get_internal_session_factory() -> async_sessionmaker[AsyncSession]:
    """Sessions for /internal reads: AUTOCOMMIT (no BEGIN and no ROLLBACK round trips; each query is one statement) and
    retried once on a dropped connection. The same pool as every other session."""
    global _internal_factory
    if _internal_factory is None:
        engine = get_engine().execution_options(isolation_level="AUTOCOMMIT")
        _internal_factory = async_sessionmaker(bind=engine, class_=ResilientSession, expire_on_commit=False, autoflush=False)
    return _internal_factory


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)
    return _session_factory


def reset_session_factory() -> None:
    """Forget the cached factory (after the engine is disposed)."""
    global _session_factory, _read_factory, _internal_factory
    _session_factory = _read_factory = _internal_factory = None


@asynccontextmanager
async def unit_of_work(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncIterator[AsyncSession]:
    factory = session_factory or get_session_factory()
    try:
        async with factory() as session:
            async with session.begin():  # commit on success, rollback on any exception
                yield session
    except IntegrityError as exc:
        mapped = map_integrity_error(exc)
        if mapped is None:
            raise
        raise mapped from exc
