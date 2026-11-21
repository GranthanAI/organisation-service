"""Throwaway databases in the shared dev Postgres (no Testcontainers).

One database `org_test_<8 hex>` per test FILE: created, migrated with the real Alembic chain, dropped at module
teardown even when tests fail. Everything here is synchronous (alembic's env.py calls asyncio.run), so use it from
sync fixtures only.
"""

import asyncio
import os
import re
import uuid
from collections.abc import Callable

import asyncpg
from alembic import command
from alembic.config import Config

from app.core.config import settings
from scripts.create_db import parse

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREFIX = "org_test_"
_NAME = re.compile(r"^org_test_[0-9a-f]{8}$")


def _admin_args() -> dict:
    args = parse(settings.DATABASE_URL)
    args.pop("database")
    return args


def _run(coro_factory: Callable):
    async def go():
        conn = await asyncpg.connect(database="postgres", **_admin_args())
        try:
            return await coro_factory(conn)
        finally:
            await conn.close()

    return asyncio.run(go())


def new_database_name() -> str:
    return PREFIX + uuid.uuid4().hex[:8]


def url_for(name: str) -> str:
    a = _admin_args()
    return f"postgresql+asyncpg://{a['user']}:{a['password']}@{a['host']}:{a['port']}/{name}"


def create_database(name: str) -> None:
    assert _NAME.match(name), name
    _run(lambda conn: conn.execute(f'CREATE DATABASE "{name}"'))


def drop_database(name: str) -> None:
    assert _NAME.match(name), name  # never drop anything that is not a throwaway
    _run(lambda conn: conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def leftover_databases() -> list[str]:
    rows = _run(lambda conn: conn.fetch("SELECT datname FROM pg_database WHERE datname LIKE 'org\\_test\\_%'"))
    return [r["datname"] for r in rows]


def alembic_config(url: str) -> Config:
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "app", "db", "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str = "base") -> None:
    command.downgrade(alembic_config(url), revision)
