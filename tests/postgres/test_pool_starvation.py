"""A request must never hold two database connections at once. With a pool of 4 (and no overflow) 40 concurrent accepts used to
deadlock on each other (each held one connection for authentication and waited for a second one for its transaction): every one
of them timed out after 30 s and answered 500. Found by the 100-way seat race load script; this is its regression test."""

import asyncio
import time
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.invite import Invite
from tests.api.conftest import api, clock  # noqa: F401  (fixtures)
from tests.support.factories import ensure_known
from tests.support.invites import seed_invite
from tests.support.tokens import bearer, make_token

POOL = 4


@pytest.fixture
async def engine(database_url):
    """The shared `engine` fixture, but with a pool of 4 and no overflow: any request that needs two connections at once starves."""
    eng = create_async_engine(database_url, pool_size=POOL, max_overflow=0, pool_timeout=20)
    async with eng.begin() as conn:
        tables = (await conn.execute(text(
            "SELECT string_agg(format('%I', tablename), ', ') FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"))).scalar_one()
        if tables:
            await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    yield eng
    await eng.dispose()


async def accept(api, token, headers):  # noqa: F811
    return await api.post("/org/v1/invites/accept", {"token": token}, headers)


async def test_forty_concurrent_accepts_through_a_pool_of_four_all_finish(api):  # noqa: F811
    world = await api.seed_org(seats=25, admins=0, members=0)                     # the owner plus 24 free seats
    people = []
    for i in range(40):
        email = f"racer{i}@amity.edu"
        user = await ensure_known(api.sf, uuid.uuid4(), email)
        _, token = await seed_invite(api.sf, world.org.id, email=email)
        people.append((token, bearer(make_token(sub=user, email=email))))
    started = time.perf_counter()
    results = await asyncio.gather(*[accept(api, token, headers) for token, headers in people])
    elapsed = time.perf_counter() - started
    codes = sorted(r.status_code for r in results)
    assert set(codes) <= {200, 409}, codes                                         # not one 500 or 503
    assert codes.count(200) == 24 and codes.count(409) == 16
    assert all(r.json()["code"] == "SEATS_FULL" for r in results if r.status_code == 409)
    assert elapsed < 15, f"the pool was starved: {elapsed:.1f} s"


async def test_concurrent_accepts_of_expired_invites_do_not_take_a_second_connection(api):  # noqa: F811
    world = await api.seed_org()
    people = []
    for i in range(20):
        email = f"late{i}@amity.edu"
        user = await ensure_known(api.sf, uuid.uuid4(), email)
        invite, token = await seed_invite(api.sf, world.org.id, email=email, expires_in=-timedelta(seconds=1))
        people.append((invite.id, token, bearer(make_token(sub=user, email=email))))
    results = await asyncio.gather(*[accept(api, token, headers) for _, token, headers in people])
    assert {r.status_code for r in results} == {410}
    async with api.sf() as s:
        closed = (await s.execute(select(Invite).where(Invite.id.in_([i for i, _, _ in people])))).scalars().all()
    assert all(i.revoke_reason == "expired" and i.revoked_at is not None for i in closed)


async def test_a_mix_of_reads_and_writes_keeps_flowing_on_a_tiny_pool(api):  # noqa: F811
    world = await api.seed_org(admins=1, members=3)
    reads = [api.client.get("/org/v1/me", headers=api.h(world.members[0])) for _ in range(30)]
    members = [api.client.get(f"/org/v1/orgs/{world.org.id}/members", headers=api.h(world.admin)) for _ in range(30)]
    writes = [api.post(f"/org/v1/orgs/{world.org.id}/invites", {"emails": [f"w{i}@amity.edu"]}, api.h(world.owner)) for i in range(30)]
    results = await asyncio.gather(*reads, *members, *writes)
    assert {r.status_code for r in results} == {200}


async def test_an_exhausted_pool_is_a_503_with_retry_after_not_a_500(api, monkeypatch):  # noqa: F811
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    from app.services import me_service

    async def starved(*args, **kwargs):
        raise PoolTimeout("QueuePool limit of size 4 overflow 0 reached, connection timed out, timeout 10.00")

    world = await api.seed_org()
    monkeypatch.setattr(me_service, "get_me", starved)
    r = await api.client.get("/org/v1/me", headers=api.h(world.owner))
    assert r.status_code == 503 and r.json()["code"] == "UPSTREAM_UNAVAILABLE" and r.headers["retry-after"] == "1"
