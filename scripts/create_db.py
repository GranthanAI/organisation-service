"""Create organisation_service_db in the shared Postgres. Safe to run twice.

Connects to the maintenance database `postgres` with the credentials from DATABASE_URL.
"""

import asyncio
import re
import sys

import asyncpg

from app.core.config import settings


def parse(url: str) -> dict:
    m = re.match(r"^postgresql(?:\+asyncpg)?://([^:@/]+)(?::([^@]*))?@([^:/]+)(?::(\d+))?/([^?]+)", url)
    if not m:
        raise SystemExit("DATABASE_URL is not a postgresql URL")
    user, password, host, port, database = m.groups()
    return {"user": user, "password": password, "host": host, "port": int(port or 5432), "database": database}


async def main() -> int:
    target = parse(settings.DATABASE_URL)
    name = target.pop("database")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise SystemExit(f"refusing unsafe database name: {name!r}")
    conn = await asyncpg.connect(database="postgres", **target)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name)
        if exists:
            print(f"database {name} already exists")
        else:
            await conn.execute(f'CREATE DATABASE "{name}"')
            print(f"database {name} created")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
