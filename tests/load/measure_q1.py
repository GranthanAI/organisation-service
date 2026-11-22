"""Where does the time go in GET /internal/memberships/{id}? (Q1) Measured in this process against the dev database:

    uv run python -m tests.load.measure_q1

1. raw asyncpg "SELECT 1" round trip;   2. the membership query alone on a raw asyncpg connection;
3. the same query through the app's SQLAlchemy engine and session (what get_db_session does);
4. the full HTTP call through the ASGI app (httpx ASGITransport, service token).
Each at concurrency 1 and 20. Needs `make db-migrate` and `uv run python -m scripts.seed_dev` data (it seeds one org itself).
"""

import asyncio
import json
import time

import asyncpg
import httpx

from app.core.config import settings
from app.db.postgres import dispose_engine
from app.main import app
from scripts import seed_dev
from tests.load.stats import line, summarise

QUERY = ("SELECT m.org_id, m.role, o.type, o.status FROM memberships m JOIN organisations o ON o.id = m.org_id "
         "WHERE m.user_id = $1 AND m.status = 'active'")
SEQUENTIAL, WAVES, WIDTH = 600, 40, 20
TOKEN = {"Authorization": "Bearer dev-billing-token"}


def dsn() -> str:
    return settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


async def sequential(call, n=SEQUENTIAL):
    out = []
    for _ in range(n):
        started = time.perf_counter()
        await call()
        out.append(time.perf_counter() - started)
    return out


async def concurrent(call, waves=WAVES, width=WIDTH):
    out = []

    async def one():
        started = time.perf_counter()
        await call()
        out.append(time.perf_counter() - started)

    for _ in range(waves):
        await asyncio.gather(*[one() for _ in range(width)])
    return out


async def main() -> None:
    ids = await seed_dev.main()
    user = ids["member_id"]
    results = {}

    # 1 and 2: raw asyncpg, one connection per concurrent caller
    conns = [await asyncpg.connect(dsn()) for _ in range(WIDTH)]
    pool = list(conns)

    async def raw_select1():
        c = pool.pop()
        try:
            await c.fetchval("SELECT 1")
        finally:
            pool.append(c)

    async def raw_query():
        c = pool.pop()
        try:
            await c.fetchrow(QUERY, __import__("uuid").UUID(user))
        finally:
            pool.append(c)

    # 3: the app's engine and session
    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.membership import Membership
    from app.models.organisation import Organisation

    async def engine_query():
        async with get_session_factory()() as session:
            await session.execute(select(Membership.org_id, Membership.role, Organisation.type, Organisation.status)
                                  .join(Organisation, Organisation.id == Membership.org_id)
                                  .where(Membership.user_id == __import__("uuid").UUID(user), Membership.status == "active"))
            await session.rollback()

    # 4: the full call
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://org") as client:
        async def http_call():
            r = await client.get(f"/internal/memberships/{user}", headers=TOKEN)
            assert r.status_code == 200, r.text

        steps = [("1 raw SELECT 1", raw_select1), ("2 membership query, raw asyncpg", raw_query),
                 ("3 membership query, SQLAlchemy engine+session", engine_query), ("4 full HTTP call (ASGI)", http_call)]
        for label, call in steps:
            for _ in range(30):
                await call()                                    # warm up
            results[f"{label} c=1"] = summarise(await sequential(call))
            results[f"{label} c=20"] = summarise(await concurrent(call))
    for c in conns:
        await c.close()
    await dispose_engine()
    for label, summary in results.items():
        print(line(label, summary))
    print(json.dumps(results))


if __name__ == "__main__":
    asyncio.run(main())
