"""Fixtures for tests/postgres: ONE throwaway migrated database per test file, a clean schema per test."""

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.support import db as testdb


@pytest.fixture(scope="module")
def database_url():
    """org_test_<8 hex>, migrated to head; dropped at module teardown even if tests fail."""
    name = testdb.new_database_name()
    testdb.create_database(name)
    try:
        url = testdb.url_for(name)
        testdb.upgrade(url)
        yield url
    finally:
        testdb.drop_database(name)


@pytest_asyncio.fixture
async def engine(database_url):
    eng = create_async_engine(database_url, pool_size=10, max_overflow=10)
    async with eng.begin() as conn:
        tables = (await conn.execute(text(
            "SELECT string_agg(format('%I', tablename), ', ') FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"))).scalar_one()
        if tables:
            await conn.execute(text(f"TRUNCATE {tables} CASCADE"))  # TRUNCATE is not guarded; DELETE/UPDATE are
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine):
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def session(session_factory):
    """A session in one transaction that is rolled back at the end of the test."""
    async with session_factory() as s:
        yield s
        await s.rollback()
