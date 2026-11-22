"""PostgreSQL engine (SQLAlchemy 2 async). One engine per process, bounded pool, per-statement timeout.

Runtime role is DML only; schema changes go through Alembic (`make db-migrate`), never at startup.
"""

from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import settings

_engine: Optional[AsyncEngine] = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.DATABASE_URL,
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_pre_ping=settings.DB_PRE_PING,
            pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
            connect_args={
                "timeout": settings.DB_CONNECT_TIMEOUT_SECONDS,
                "server_settings": {"statement_timeout": str(settings.DB_STATEMENT_TIMEOUT_MS)},
            },
        )
    return _engine


async def check_database() -> bool:
    """Readiness probe: open a connection and run SELECT 1."""
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
