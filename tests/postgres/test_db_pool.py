"""Q1's correctness bits: a read retries once on a dropped pooled connection, /internal reads are autocommit, and the
pool defaults are what lld.md says."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db import session as db_session
from app.db.session import ResilientSession


@pytest.fixture
async def fresh_engine(database_url):
    engine = create_async_engine(database_url, pool_size=2, max_overflow=0, pool_pre_ping=False)
    yield engine
    await engine.dispose()


async def kill_pooled_connections(database_url):
    """Terminate every backend of this test database except ours: the pool still holds the dead sockets."""
    admin = create_async_engine(database_url)
    try:
        async with admin.connect() as conn:
            await conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                    "WHERE datname = current_database() AND pid <> pg_backend_pid()"))
    finally:
        await admin.dispose()


async def test_a_read_retries_once_when_the_pooled_connection_was_dropped(fresh_engine, database_url):
    factory = async_sessionmaker(bind=fresh_engine, class_=ResilientSession, expire_on_commit=False)
    async with factory() as s:
        assert (await s.execute(text("SELECT 1"))).scalar_one() == 1               # a connection is now in the pool
    await kill_pooled_connections(database_url)                                    # ...and it is dead
    async with factory() as s:
        assert (await s.execute(text("SELECT 2"))).scalar_one() == 2               # retried on a fresh connection
    async with factory() as s:
        assert (await s.execute(text("SELECT 3"))).scalar_one() == 3               # the pool recovered as a whole


async def test_a_plain_session_fails_once_on_a_dropped_connection_which_is_why_reads_retry(fresh_engine, database_url):
    plain = async_sessionmaker(bind=fresh_engine, expire_on_commit=False)
    async with plain() as s:
        await s.execute(text("SELECT 1"))
    await kill_pooled_connections(database_url)
    with pytest.raises(Exception) as caught:
        async with plain() as s:
            await s.execute(text("SELECT 2"))
    assert getattr(caught.value, "connection_invalidated", False)


async def test_a_real_error_is_not_retried(fresh_engine):
    calls = []
    factory = async_sessionmaker(bind=fresh_engine, class_=ResilientSession, expire_on_commit=False)
    async with factory() as s:
        with pytest.raises(Exception) as caught:
            await s.execute(text("SELECT * FROM table_that_does_not_exist"))
        calls.append(caught.value)
    assert "table_that_does_not_exist" in str(calls[0]) and not getattr(calls[0], "connection_invalidated", False)


async def test_get_retries_too(fresh_engine, database_url):
    from app.models.organisation import Organisation
    factory = async_sessionmaker(bind=fresh_engine, class_=ResilientSession, expire_on_commit=False)
    async with factory() as s:
        await s.execute(text("SELECT 1"))
    await kill_pooled_connections(database_url)
    async with factory() as s:
        assert await s.get(Organisation, __import__("uuid").uuid4()) is None


async def test_the_internal_session_is_autocommit_and_the_read_session_is_a_transaction(fresh_engine, monkeypatch):
    monkeypatch.setattr(db_session, "get_engine", lambda: fresh_engine)
    db_session.reset_session_factory()
    try:
        async def state_of(factory):
            async with factory() as s:
                pid = (await s.execute(text("SELECT pg_backend_pid()"))).scalar_one()
                async with fresh_engine.connect() as other:
                    return (await other.execute(text("SELECT state FROM pg_stat_activity WHERE pid = :p"), {"p": pid})).scalar_one()

        assert await state_of(db_session.get_internal_session_factory()) == "idle"                     # no BEGIN was sent
        assert await state_of(db_session.get_read_session_factory()) == "idle in transaction"           # a read transaction
    finally:
        db_session.reset_session_factory()


def test_the_pool_defaults_and_the_pre_ping_choice():
    fresh = Settings(_env_file=None)
    assert (fresh.DB_POOL_SIZE, fresh.DB_MAX_OVERFLOW, fresh.DB_PRE_PING, fresh.DB_POOL_RECYCLE_SECONDS) == (10, 10, False, 1800)


def test_the_engine_is_built_from_those_settings(monkeypatch):
    from app.db import postgres
    monkeypatch.setattr(postgres, "_engine", None)
    engine = postgres.get_engine()
    try:
        pool = engine.sync_engine.pool
        from app.core.config import settings
        assert pool.size() == settings.DB_POOL_SIZE and pool._max_overflow == settings.DB_MAX_OVERFLOW
        assert pool._pre_ping is settings.DB_PRE_PING and pool._recycle == settings.DB_POOL_RECYCLE_SECONDS
    finally:
        monkeypatch.setattr(postgres, "_engine", None)
