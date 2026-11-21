"""Async sessions and the unit of work: ONE transaction per use case.

Repositories take the session and never commit. `unit_of_work()` commits when the block ends normally and rolls
everything back (membership, outbox and audit rows alike) on any exception. A constraint violation that
`app/db/errors.py` knows becomes its domain error (for example AlreadyInOrg).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.errors import map_integrity_error
from app.db.postgres import get_engine

_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)
    return _session_factory


def reset_session_factory() -> None:
    """Forget the cached factory (after the engine is disposed)."""
    global _session_factory
    _session_factory = None


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
